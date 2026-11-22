"""The screen for the sponsorship agent: `streamlit run app.py`.

Describe the event -> see the brief, the host check and ranked sponsors with their
evidence -> tick companies -> read each pitch -> approve -> pipeline. All work is done
by agent.py; this file only shows it and collects the host's choices.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import streamlit as st

import agent
import outreach
from brief import BriefIncomplete, ExtractionFailed
from crustdata_client import BudgetExceeded, NotInCache

EXAMPLES = Path(__file__).resolve().parent / "demo"
SIGNAL_LABELS = {"past_sponsorships": "past sponsor", "active_hiring": "hiring", "city_presence": "in city",
                 "budget": "budget", "audience_fit": "audience", "reachable_contact": "contact"}
SHOWN = 30  # candidates in the table

st.set_page_config(page_title="Sponsor agent", layout="centered")
st.markdown("""<style>
  #MainMenu, header, footer {visibility: hidden;}
  .block-container {padding-top: 2.5rem; max-width: 820px;}
  h1 {font-weight: 600; letter-spacing: -0.02em;}
  h3 {font-weight: 500; margin-top: 2rem;}
</style>""", unsafe_allow_html=True)

st.title("Sponsor agent")
st.caption("Finds, checks and pitches sponsors for your event. Every fact has a source.")


# -- input --------------------------------------------------------------------

examples = {p.stem.replace("_", " "): p.read_text(encoding="utf-8") for p in sorted(EXAMPLES.glob("*.txt"))}
example = st.selectbox("Example", list(examples), index=None, placeholder="Load an example",
                       label_visibility="collapsed")
text = st.text_area("Describe your event", value=examples.get(example, ""), height=180,
                    placeholder="Name, type, date, city, audience, host and website, sponsorship tiers, "
                                "past sponsors, kinds of company to avoid.")
left, right = st.columns([1, 3])
if left.button("Run", type="primary", disabled=not text.strip()):
    st.session_state.clear()
    st.session_state.description = text
offline = right.toggle("Replay from cache (no API calls)")

if "description" not in st.session_state:
    st.stop()

job = agent.open_agent(st.session_state.description, offline=offline)


def rupees(amount: int) -> str:
    """500000 -> '₹5 lakh', 2500000 -> '₹25 lakh', 75000 -> '₹75,000'."""
    return f"₹{amount / 100_000:g} lakh" if amount >= 100_000 else f"₹{amount:,}"


def stop_with(message: str) -> None:
    st.info(message)
    st.stop()


# -- brief and host -----------------------------------------------------------

try:
    with st.spinner("Reading the description"):
        brief, dropped = agent.make_brief(job, st.session_state.description)
    with st.spinner("Checking the host"):
        host = agent.check_host(job, brief)
    with st.spinner("Finding sponsors: job posts, past sponsorships, company search (a few minutes the first time)"):
        ranked, failed = agent.find_candidates(job, brief)
except BriefIncomplete as e:
    stop_with(f"Please add to the description: {', '.join(e.missing).replace('_', ' ')}.")
except ExtractionFailed:
    stop_with("Couldn't read the description. Try rewording it.")
except (agent.NotCached, NotInCache):
    stop_with("This step isn't in the cache. Turn off replay to run it live.")
except BudgetExceeded as e:
    stop_with(f"Stopped: {e}.")

st.subheader("Brief")
rows = {
    "Event": brief.event_name, "Type": brief.profile, "Date": brief.event_date, "City": brief.city,
    "Audience": f"{brief.audience_size:,} · {brief.audience}" if brief.audience_size else brief.audience,
    "Host": f"{brief.host_name} ({brief.host_website})" if brief.host_website else brief.host_name,
    "Tiers": ", ".join(f"{t.name} {rupees(t.price_inr)}" if t.price_inr else t.name for t in brief.tiers),
    "Past sponsors": ", ".join(brief.past_sponsors), "Avoid": ", ".join(brief.excluded_categories),
}
st.markdown("\n".join(f"- **{k}** {v}" for k, v in rows.items() if v))
if dropped:
    with st.expander(f"{len(dropped)} detail(s) not used: not found word for word in the description"):
        for d in dropped:
            st.write(f"{d['field']}: {d['value']} ({d['reason']})")

if host.status == "verified":
    st.markdown(f"**Host verified.** {host.source_url} names {brief.host_name}.")
else:
    st.warning(f"Host not verified: {host.missing}. Outreach stays blocked.")


# -- candidates ---------------------------------------------------------------

st.subheader("Sponsors")
if failed:
    st.warning(f"These sources failed, so the list may be incomplete: {', '.join(failed)}.")
    if not st.checkbox("A person has reviewed this. Continue."):
        st.stop()


def signals_text(candidate) -> str:
    return " · ".join(f"{SIGNAL_LABELS.get(s.name, s.name)} {s.value:.2f}"
                      for s in candidate.signals if s.value is not None)


scored = [c for c in ranked if c.excluded_reason is None][:SHOWN]
excluded = [c for c in ranked if c.excluded_reason]
st.caption(f"{len(ranked)} companies found. Top {len(scored)} shown. Score uses only sourced facts; "
           "confidence is the share of the score's inputs that were known.")
table = st.data_editor(
    [{"Pitch": False, "Company": c.name, "Score": c.score, "Confidence": c.confidence,
      "Why": signals_text(c), "Found by": ", ".join(c.found_by).replace("_", " ")} for c in scored],
    column_config={"Pitch": st.column_config.CheckboxColumn(width="small"),
                   "Score": st.column_config.NumberColumn(format="%.2f"),
                   "Confidence": st.column_config.NumberColumn(format="%.2f")},
    disabled=["Company", "Score", "Confidence", "Why", "Found by"], hide_index=True, width="stretch")
picked = [c for c, row in zip(scored, table) if row["Pitch"]]

with st.expander("Evidence for a company"):
    choice = st.selectbox("Company", scored, format_func=lambda c: c.name, label_visibility="collapsed")
    for s in choice.signals if choice else []:
        for f in s.evidence:
            mark = "Verified" if f.status == "verified" else f"Cannot verify: {f.missing}"
            st.markdown(f"**{mark}.** {f.claim}  \n“{f.quote}” · [{f.source_type}]({f.source_url}) · "
                        f"{(f.record_date or 'no date')[:10]}")
if excluded:
    with st.expander(f"{len(excluded)} excluded"):
        for c in excluded:
            st.write(f"{c.name}: {c.excluded_reason}")


# -- pitches ------------------------------------------------------------------

st.subheader("Pitches")
if st.button(f"Find contacts and draft {len(picked)} pitch(es)", disabled=not picked or offline):
    st.session_state.pitch_ids = [c.company_id for c in picked]
    with st.spinner("Finding decision-makers and drafting"):
        selected = [(c, agent.find_contact(job, c)) for c in picked]
        try:
            agent.draft_pitches(job, brief, host, selected)
        except outreach.OutreachBlocked as e:
            stop_with(str(e))

by_id = {c.company_id: c for c in ranked}
progress = agent.pipeline(job)
state = {r["company_id"]: r["state"] for r in progress}
pitch_ids = st.session_state.get("pitch_ids") or list(state)   # after a reload: this run's earlier pitches
selected = [(by_id[i], agent.find_contact(job, by_id[i])) for i in pitch_ids if i in by_id]
drafts = [d for c, _ in selected if (d := outreach.saved_pitch(job.db, job.run_id, c, brief))]
if not drafts:
    st.caption("Tick companies above, then draft. Contacts are looked up only for the companies you pick.")

approved = set()
for d in drafts:
    with st.container(border=True):
        st.markdown(f"**{d.company_name}** · to {d.to_role}  \n*{d.subject}*")
        st.text(d.body)
        for note in d.notes:
            st.caption(note)
        if state.get(d.company_id, "drafted") != "drafted":
            st.caption(f"Status: {state[d.company_id].replace('_', ' ')}")
        elif st.checkbox("Approve", key=f"approve-{d.company_id}"):
            approved.add(d.company_id)

if drafts:
    if not job.email_ready:
        st.caption("Gmail isn't set up on this machine, so approved emails are written to outbox/ instead.")
    if st.button(f"Send {len(approved)} approved", disabled=not approved or offline):
        agent.send_approved(job, brief, host, selected, approved)
        st.rerun()

if progress:
    st.subheader("Pipeline")
    st.dataframe([{"Company": r["company"], "State": r["state"].replace("_", " "), "Contact": r["contact"],
                   "Why": r["why"]} for r in progress], hide_index=True, width="stretch")
    st.download_button("Download brief.json", json.dumps(asdict(brief), ensure_ascii=False, indent=2),
                       file_name="brief.json")
    st.caption(f"Replies: `python outreach.py replies {job.run_id} brief.json`. A person approves each answer.")

spent = agent.cost(job)
st.divider()
st.caption(f"Run {job.run_id} · {spent['credits']} Crustdata credits · ${spent['model_usd']} model · "
           f"{'replaying from cache' if offline else 'live'}")
