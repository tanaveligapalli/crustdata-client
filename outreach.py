"""Block 6: outreach and replies. From an approved shortlist to booked meetings.

Flow for each selected sponsor:
  find the decision-maker -> draft a pitch -> the host approves it (CLI) -> send ->
  read replies -> answer questions from the brief only / book a meeting / hand
  negotiation to the host / stop on opt-out -> at most 2 follow-ups -> no_response.

Rules this file enforces in code, not just in prompts:
- Every email goes to the TEAM INBOX. The real target is only named in the subject:
  "[to: Head of Partnerships, Acme]". We never email a real company during the event.
- Nothing is sent without a person typing "y" at the approval prompt.
- Every send is steps.do_once(...), so killing the process and restarting never sends twice.
  If a send started but we can't tell whether it finished, the company goes to needs_person.
- The model only drafts and classifies. Any number in a draft or an answer must already
  appear in the brief or the sourced evidence, otherwise the draft is dropped
  (pitch: plain template instead; answer: handed to the host).
- An opt-out always wins, is kept across runs, and that company is never emailed again.
- Every outside call (model, SMTP, IMAP) goes in the cost ledger.
"""

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import smtplib
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable

import ledger
from agent_db import now_iso
from records import OUTREACH_STATES, Brief, Candidate, Contact, Evidence
from steps import ActionNeedsCheck, do_once, run_step

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

FINISHED = {"meeting_booked", "declined", "opted_out", "no_response"}
REPLY_STATES = ["replied_question", "replied_interested", "negotiating", "declined", "opted_out"]
REF_RE = re.compile(r"\[ref:([^\]\s]+):(\d+)\]")

# A model call: (system, user, json_schema) -> (text, cost_usd). Tests pass a fake.
ModelCall = Callable[[str, str, dict], tuple[str, float | None]]


class OutreachBlocked(Exception):
    """Outreach may not start, e.g. the host organization is not verified."""


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)["outreach"]
    # Addresses can live in env vars so nobody commits the team account.
    config["sender"] = os.environ.get("OUTREACH_SENDER", config["sender"])
    config["team_inbox"] = os.environ.get("OUTREACH_TEAM_INBOX", config["team_inbox"])
    if "example.com" in config["sender"] + config["team_inbox"]:
        raise ValueError("set OUTREACH_SENDER and OUTREACH_TEAM_INBOX (or outreach.sender/team_inbox in config.yaml)")
    return config


# -- database: pipeline state, history, opt-outs, replies already handled ---------

def ensure_tables(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS outreach (
        run_id       TEXT NOT NULL,
        company_id   INTEGER NOT NULL,
        company_name TEXT NOT NULL,
        state        TEXT NOT NULL,
        contact_json TEXT,
        follow_ups   INTEGER NOT NULL DEFAULT 0,
        last_sent_at TEXT,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (run_id, company_id)
    );
    CREATE TABLE IF NOT EXISTS outreach_events (   -- the "why" behind each state
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id     TEXT NOT NULL,
        company_id INTEGER NOT NULL,
        state      TEXT NOT NULL,
        reason     TEXT NOT NULL,
        at         TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS opt_outs (           -- kept across runs on purpose
        company_id   INTEGER PRIMARY KEY,
        company_name TEXT NOT NULL,
        at           TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pitch_drafts (       -- the pitch shown for approval is the one sent
        run_id     TEXT NOT NULL,
        company_id INTEGER NOT NULL,
        to_role    TEXT NOT NULL,
        subject    TEXT NOT NULL,
        body       TEXT NOT NULL,
        notes_json TEXT NOT NULL,
        PRIMARY KEY (run_id, company_id)
    );
    CREATE TABLE IF NOT EXISTS replies_seen (
        message_id TEXT PRIMARY KEY,
        run_id     TEXT NOT NULL,
        company_id INTEGER NOT NULL,
        state      TEXT NOT NULL,
        at         TEXT NOT NULL
    );
    """)
    db.commit()


def get_row(db: sqlite3.Connection, run_id: str, company_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM outreach WHERE run_id = ? AND company_id = ?",
                      (run_id, company_id)).fetchone()


def set_state(db: sqlite3.Connection, run_id: str, company_id: int, state: str, reason: str) -> bool:
    """Move a company to `state` and log why. A finished deal stays finished, except opt-out."""
    if state not in OUTREACH_STATES:
        raise ValueError(f"unknown outreach state {state!r}")
    row = get_row(db, run_id, company_id)
    if row and row["state"] in FINISHED and state != "opted_out":
        return False
    db.execute("UPDATE outreach SET state = ?, updated_at = ? WHERE run_id = ? AND company_id = ?",
               (state, now_iso(), run_id, company_id))
    db.execute("INSERT INTO outreach_events (run_id, company_id, state, reason, at) VALUES (?, ?, ?, ?, ?)",
               (run_id, company_id, state, reason, now_iso()))
    db.commit()
    return True


def is_opted_out(db: sqlite3.Connection, company_id: int) -> bool:
    return db.execute("SELECT 1 FROM opt_outs WHERE company_id = ?", (company_id,)).fetchone() is not None


def pipeline(db: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Each company's state and the latest reason: the finished result the host sees."""
    ensure_tables(db)
    rows = db.execute("SELECT * FROM outreach WHERE run_id = ? ORDER BY company_name", (run_id,)).fetchall()
    out = []
    for r in rows:
        why = db.execute("""SELECT reason FROM outreach_events WHERE run_id = ? AND company_id = ?
                            ORDER BY id DESC LIMIT 1""", (run_id, r["company_id"])).fetchone()
        contact = json.loads(r["contact_json"]) if r["contact_json"] else None
        out.append({"company_id": r["company_id"], "company": r["company_name"], "state": r["state"],
                    "contact": f"{contact['name']}, {contact['title']}" if contact else None,
                    "follow_ups": r["follow_ups"], "why": why["reason"] if why else ""})
    return out


# -- contacts --------------------------------------------------------------------

def find_contact(client, candidate: Candidate, config: dict[str, Any]) -> Contact | None:
    """The best decision-maker at the company, by the role order in config. Business details only."""
    found = find_contacts(client, candidate, config)
    return found[0] if found else None


def find_contacts(client, candidate: Candidate, config: dict[str, Any]) -> list[Contact]:
    """Everyone at the company who holds one of the config roles now, best role first.

    The host picks one of them to pitch (app.py). Same request as find_contact, so the
    list costs nothing extra. Business details only: business emails need
    /person/contact/enrich, which is not on our plan, so business_email stays None.
    During the event every email goes to the team inbox anyway.
    """
    roles = config["contact_roles"]
    body = {
        "filters": {"op": "and", "conditions": [
            {"field": "experience.employment_details.current.company_id", "type": "=",
             "value": candidate.company_id},
            {"op": "or", "conditions": [
                {"field": "experience.employment_details.current.title", "type": "(.)", "value": role}
                for role in roles]},
        ]},
        "fields": ["crustdata_person_id", "basic_profile", "social_handles",
                   "experience.employment_details.current"],
        "limit": 10,
    }
    response = client.call("/person/search", body)
    ranked = []
    for order, person in enumerate((response.data or {}).get("profiles", [])):
        job = job_here(person, candidate.company_id)
        if job is None:
            continue                       # left the company, or their current job is elsewhere
        title = job.get("title") or ""
        if any(has_words(title, word) for word in config.get("contact_exclude_titles", [])):
            continue                       # "Private Equity Partnerships" is not brand partnerships
        rank = next((i for i, role in enumerate(roles) if has_words(title, role)), None)
        if rank is not None:
            ranked.append((rank, order, person, job))
    ranked.sort(key=lambda r: (r[0], r[1]))   # best role first; ties keep the search order

    contacts = []
    for _rank, _order, person, job in ranked:
        name, title, start = person["basic_profile"]["name"], job["title"], job.get("start_date")
        url = ((person.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url", "")
        evidence = Evidence(
            claim=f"{name} is {title} at {candidate.name}",
            value=title,
            source_url="crustdata:/person/search",
            quote=f"experience.employment_details.current[].title = {title}",
            record_date=start[:10] if start else None,
            retrieved_at=response.retrieved_at,
            source_type="crustdata:/person/search",
        )
        contacts.append(Contact(company_id=candidate.company_id, name=name, title=title,
                                profile_url=url, business_email=None, evidence=[evidence]))
    return contacts


def job_here(person: dict[str, Any], company_id: int) -> dict[str, Any] | None:
    """The person's current job at this company: same company ID and no end date.

    The headline title can be about another job, and a profile can still list a job
    the person has left (end date set): neither makes them our contact.
    """
    jobs = ((person.get("experience") or {}).get("employment_details") or {}).get("current") or []
    return next((j for j in jobs if j and j.get("crustdata_company_id") == company_id
                 and not j.get("end_date") and j.get("title")), None)


def has_words(text: str, words: str) -> bool:
    """Whole words, plural allowed: "Partnership" is in "Partnerships Lead", "intern" is not in
    "International". No plural for short acronyms: "CMO" must not match "CMOS"."""
    parts = words.split()
    plural = "s?" if len(parts[-1]) > 3 else ""
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in parts) + plural + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


# -- fact checks -------------------------------------------------------------------

def brief_facts(brief: Brief) -> dict[str, Any]:
    """The only facts the agent may tell a sponsor. Numbers are written the way we allow them."""
    tiers = [{"name": t.name,
              "price": f"₹{t.price_inr:,}" if t.price_inr is not None else "price on request",
              "benefits": t.benefits} for t in brief.tiers]
    return {"event_name": brief.event_name, "event_date": brief.event_date, "city": brief.city,
            "audience_size": brief.audience_size, "audience": brief.audience,
            "host_name": brief.host_name, "host_website": brief.host_website, "tiers": tiers}


def numbers_in(text: str) -> set[str]:
    """Every number in the text, commas removed: '₹5,00,000' -> '500000', '14 March 2027' -> 14, 2027."""
    return {n.replace(",", "").lstrip("0") or "0" for n in re.findall(r"\d[\d,]*", text)}


def unsupported_numbers(draft: str, allowed: list[str]) -> set[str]:
    """Numbers in the draft that no allowed text contains. Non-empty = the draft invented something."""
    known = set()
    for text in allowed:
        known |= numbers_in(text)
    return numbers_in(draft) - known


def _allowed_texts(brief: Brief, candidate: Candidate | None = None) -> list[str]:
    texts = [json.dumps(brief_facts(brief), ensure_ascii=False)]
    for ev in verified_evidence(candidate) if candidate else []:
        texts += [ev.claim, ev.quote]
    return texts


def verified_evidence(candidate: Candidate) -> list[Evidence]:
    return [ev for s in candidate.signals for ev in s.evidence if ev.status == "verified"]


# -- drafting ----------------------------------------------------------------------

@dataclass
class Draft:
    company_id: int
    company_name: str
    kind: str                     # intro, follow_up_1, answer:<id>, invite, handoff
    to_role: str                  # "Head of Partnerships, Acme" -> goes in the subject tag
    subject: str
    body: str
    attachments: list[tuple[str, str, str]] = field(default_factory=list)  # (filename, text, mime subtype)
    in_reply_to: str | None = None
    notes: list[str] = field(default_factory=list)   # shown to the approver, never sent
    event_key: str = ""           # which event this pitch is for; part of the once-only key


def event_key(brief: Brief) -> str:
    """Names the event, not the run, so a new run ID can't email the same company twice for it."""
    parts = [brief.host_name, brief.event_name, brief.event_date or ""]
    return "-".join(re.sub(r"[^a-z0-9]+", "-", p.casefold()).strip("-") for p in parts)


def once_key(draft: Draft) -> str:
    """One email per company, kind and event: stable across runs (CONTEXT.md: email:{company_id}:{kind})."""
    return ":".join(p for p in ("email", draft.event_key, str(draft.company_id), draft.kind) if p)


PITCH_SCHEMA = {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                "required": ["subject", "body"], "additionalProperties": False}
PITCH_SYSTEM = """You write a short, warm first sponsorship email from an event host to a company.
Use ONLY the facts given: the event facts and the evidence about the company. Copy every number
exactly as written there. Do not invent numbers, dates, past sponsorships or claims.
Under 150 words. No sign-off line about opting out (it is added automatically).
Return JSON {"subject": ..., "body": ...}."""


def template_pitch(candidate: Candidate, contact: Contact | None, brief: Brief) -> tuple[str, str]:
    """The fallback pitch: plain, and every fact in it is from the brief or the evidence."""
    facts = brief_facts(brief)
    hello = f"Hi {contact.name.split()[0]}," if contact else f"Hi {candidate.name} team,"
    lines = [hello, "",
             f"I'm writing from {brief.host_name} about {brief.event_name}"
             + (f" in {brief.city}" if brief.city else "")
             + (f" on {brief.event_date}" if brief.event_date else "") + "."]
    if brief.audience_size:
        lines.append(f"We expect {brief.audience_size:,} attendees: {brief.audience}.")
    else:
        lines.append(f"Our audience: {brief.audience}.")
    evidence = verified_evidence(candidate)
    if evidence:
        lines.append(f"We reached out because {evidence[0].claim}.")
    if facts["tiers"]:
        tiers = "; ".join(f"{t['name']} ({t['price']})" for t in facts["tiers"])
        lines.append(f"Sponsorship options: {tiers}.")
    lines += ["", "Would you be open to a short call?", "", brief.host_name]
    return f"Sponsoring {brief.event_name}", "\n".join(lines)


def draft_pitch(candidate: Candidate, contact: Contact | None, brief: Brief,
                model: ModelCall | None, db: sqlite3.Connection, run_id: str) -> Draft:
    to_role = f"{contact.title}, {candidate.name}" if contact else f"sponsorship team, {candidate.name}"
    notes = []
    if model:
        user = json.dumps({"event": brief_facts(brief), "company": candidate.name,
                           "contact": {"name": contact.name, "title": contact.title} if contact else None,
                           "evidence": [ev.claim for ev in verified_evidence(candidate)]}, ensure_ascii=False)
        text, cost = model(PITCH_SYSTEM, user, PITCH_SCHEMA)
        ledger.record(db, run_id, "anthropic", "messages:pitch", "ok" if text else "error", cost_usd=cost)
        draft = _parse_json(text)
        if draft and draft.get("body"):
            bad = unsupported_numbers(draft["subject"] + " " + draft["body"], _allowed_texts(brief, candidate))
            if not bad:
                return _keep(db, run_id, Draft(candidate.company_id, candidate.name, "intro", to_role,
                                               draft["subject"], draft["body"], notes=notes,
                                               event_key=event_key(brief)))
            notes.append(f"model draft dropped: numbers not in the brief or evidence: {sorted(bad)}")
        else:
            notes.append("model draft unusable: used the template")
    subject, body = template_pitch(candidate, contact, brief)
    return _keep(db, run_id, Draft(candidate.company_id, candidate.name, "intro", to_role, subject, body,
                                   notes=notes, event_key=event_key(brief)))


def _keep(db: sqlite3.Connection, run_id: str, draft: Draft) -> Draft:
    """Save the pitch: the model isn't deterministic, so a redraft could differ from what was approved."""
    ensure_tables(db)
    db.execute("INSERT OR REPLACE INTO pitch_drafts VALUES (?, ?, ?, ?, ?, ?)",
               (run_id, draft.company_id, draft.to_role, draft.subject, draft.body, json.dumps(draft.notes)))
    db.commit()
    return draft


def saved_pitch(db: sqlite3.Connection, run_id: str, candidate: Candidate, brief: Brief) -> Draft | None:
    """The pitch drafted earlier for this company, if any."""
    row = db.execute("SELECT * FROM pitch_drafts WHERE run_id = ? AND company_id = ?",
                     (run_id, candidate.company_id)).fetchone()
    if row is None:
        return None
    return Draft(candidate.company_id, candidate.name, "intro", row["to_role"], row["subject"], row["body"],
                 notes=json.loads(row["notes_json"]), event_key=event_key(brief))


def follow_up(row: sqlite3.Row, brief: Brief, number: int) -> Draft:
    contact = json.loads(row["contact_json"]) if row["contact_json"] else None
    to_role = f"{contact['title']}, {row['company_name']}" if contact else f"sponsorship team, {row['company_name']}"
    hello = f"Hi {contact['name'].split()[0]}," if contact else "Hi,"
    body = (f"{hello}\n\nFollowing up on my note about sponsoring {brief.event_name}. "
            f"Happy to share the sponsorship deck or set up a short call.\n\n{brief.host_name}")
    return Draft(row["company_id"], row["company_name"], f"follow_up_{number}", to_role,
                 f"Re: Sponsoring {brief.event_name}", body, event_key=event_key(brief))


# -- sending ---------------------------------------------------------------------

def tagged_subject(draft: Draft, run_id: str) -> str:
    """The real target and our reference ride in the subject, so replies find their company."""
    return f"[to: {draft.to_role}] [ref:{run_id}:{draft.company_id}] {draft.subject}"


def with_opt_out(body: str, config: dict[str, Any]) -> str:
    return f"{body}\n\n--\n{config['opt_out_line']}"


def build_message(draft: Draft, run_id: str, config: dict[str, Any]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = config["sender"]
    msg["To"] = config["team_inbox"]            # always the team inbox, never the company
    msg["Subject"] = tagged_subject(draft, run_id)
    msg["Message-ID"] = make_msgid(domain="sponsor-agent.local")
    if draft.in_reply_to:
        msg["In-Reply-To"] = draft.in_reply_to
        msg["References"] = draft.in_reply_to
    msg.set_content(with_opt_out(draft.body, config))
    for filename, text, subtype in draft.attachments:
        msg.add_attachment(text.encode("utf-8"), maintype="text", subtype=subtype, filename=filename)
    return msg


def smtp_sender(config: dict[str, Any]) -> Callable[[EmailMessage], None]:
    """Send through the team Gmail account (app password in OUTREACH_APP_PASSWORD, never in git)."""
    def send(msg: EmailMessage) -> None:
        with smtplib.SMTP_SSL(config["smtp_host"], config.get("smtp_port", 465)) as smtp:
            smtp.login(config["sender"], os.environ["OUTREACH_APP_PASSWORD"])
            smtp.send_message(msg)
    return send


def approve(drafts: list[Draft], run_id: str, config: dict[str, Any],
            ask: Callable[[str], str] = input, show: Callable[[str], None] = print) -> list[Draft]:
    """Show every draft in full. Only an explicit "y" approves it."""
    approved = []
    for d in drafts:
        show("\n" + "=" * 70)
        show(f"To (real target): {d.to_role}   ->   delivered to: {config['team_inbox']}")
        show(f"Subject: {tagged_subject(d, run_id)}")
        for note in d.notes:
            show(f"NOTE: {note}")
        for filename, _, _ in d.attachments:
            show(f"Attachment: {filename}")
        show("-" * 70)
        show(with_opt_out(d.body, config))
        if ask(f"Send this {d.kind} to {d.company_name}? [y/N] ").strip().lower() == "y":
            approved.append(d)
    return approved


def send_once(db: sqlite3.Connection, run_id: str, draft: Draft, config: dict[str, Any],
              sender: Callable[[EmailMessage], None]) -> str | None:
    """Send at most once. Returns the Message-ID, or None if a person must check first."""
    if is_opted_out(db, draft.company_id):
        set_state(db, run_id, draft.company_id, "opted_out", "not sent: the company opted out")
        return None

    def action() -> dict[str, str]:
        msg = build_message(draft, run_id, config)
        sender(msg)
        ledger.record(db, run_id, "smtp", f"send:{draft.kind.split(':')[0]}", "ok")
        return {"message_id": msg["Message-ID"], "sent_at": now_iso()}

    try:
        result = do_once(db, run_id, once_key(draft), "email", action)
    except ActionNeedsCheck as e:
        set_state(db, run_id, draft.company_id, "needs_person",
                  f"{e}: check the team inbox's sent folder, don't resend blindly")
        return None
    db.execute("UPDATE outreach SET last_sent_at = ? WHERE run_id = ? AND company_id = ?",
               (result["sent_at"], run_id, draft.company_id))
    db.commit()
    return result["message_id"]


# -- the campaign ------------------------------------------------------------------

def start_campaign(db: sqlite3.Connection, run_id: str, brief: Brief,
                   selected: list[tuple[Candidate, Contact | None]], config: dict[str, Any],
                   sender: Callable[[EmailMessage], None], model: ModelCall | None = None,
                   host_check: Evidence | None = None,
                   ask: Callable[[str], str] = input, show: Callable[[str], None] = print) -> list[dict]:
    """Draft a pitch for each company the host selected, get approval, send.

    Safe to run again after a crash: drafts come back from the table, sent emails are not resent.
    """
    ensure_tables(db)
    if config.get("require_verified_host", True) and (host_check is None or host_check.status != "verified"):
        reason = host_check.missing if host_check else "host organization was not checked"
        raise OutreachBlocked(f"outreach blocked: host not verified ({reason})")

    drafts, approved_before_crash = [], []
    for candidate, contact in selected:
        if candidate.excluded_reason:
            continue
        row = get_row(db, run_id, candidate.company_id)
        if row is None:
            db.execute("""INSERT INTO outreach (run_id, company_id, company_name, state, contact_json, updated_at)
                          VALUES (?, ?, ?, 'drafted', ?, ?)""",
                       (run_id, candidate.company_id, candidate.name,
                        json.dumps(asdict(contact)) if contact else None, now_iso()))
            db.commit()
            row = get_row(db, run_id, candidate.company_id)
        if is_opted_out(db, candidate.company_id):
            set_state(db, run_id, candidate.company_id, "opted_out", "opted out earlier: never contacted again")
            continue
        if row["state"] not in ("drafted", "approved"):
            continue                                   # already sent, or finished
        draft = saved_pitch(db, run_id, candidate, brief) or draft_pitch(candidate, contact, brief, model, db, run_id)
        if row["state"] == "approved":                 # approved before a crash: don't ask again
            approved_before_crash.append(draft)
            continue
        drafts.append(draft)
        set_state(db, run_id, candidate.company_id, "drafted",
                  "pitch drafted from the brief and sourced evidence; waiting for approval")

    newly_approved = approve(drafts, run_id, config, ask, show)
    for d in newly_approved:
        set_state(db, run_id, d.company_id, "approved", "host approved the pitch")
    for d in approved_before_crash + newly_approved:
        if send_once(db, run_id, d, config, sender):
            set_state(db, run_id, d.company_id, "sent", "intro sent to the team inbox")
    return pipeline(db, run_id)


def run_follow_ups(db: sqlite3.Connection, run_id: str, brief: Brief, config: dict[str, Any],
                   sender: Callable[[EmailMessage], None], now: datetime | None = None,
                   ask: Callable[[str], str] = input, show: Callable[[str], None] = print) -> list[dict]:
    """No reply after the wait: follow up, at most max_follow_ups times, then no_response."""
    ensure_tables(db)
    now = now or datetime.now(timezone.utc)
    wait = timedelta(days=config["follow_up_after_days"])
    drafts = []
    for row in db.execute("""SELECT * FROM outreach WHERE run_id = ? AND state IN ('sent', 'follow_up_sent')""",
                          (run_id,)).fetchall():
        if not row["last_sent_at"] or now - datetime.fromisoformat(row["last_sent_at"]) < wait:
            continue
        if row["follow_ups"] >= config["max_follow_ups"]:
            set_state(db, run_id, row["company_id"], "no_response",
                      f"no reply after {row['follow_ups']} follow-ups")
            continue
        drafts.append(follow_up(row, brief, row["follow_ups"] + 1))

    for d in approve(drafts, run_id, config, ask, show):
        if send_once(db, run_id, d, config, sender):
            db.execute("UPDATE outreach SET follow_ups = ? WHERE run_id = ? AND company_id = ?",
                       (int(d.kind.rsplit("_", 1)[1]), run_id, d.company_id))
            db.commit()
            set_state(db, run_id, d.company_id, "follow_up_sent", f"{d.kind.replace('_', ' ')} sent")
    return pipeline(db, run_id)


# -- replies -----------------------------------------------------------------------

@dataclass
class Reply:
    message_id: str
    sender: str
    subject: str
    body: str


def new_text(body: str) -> str:
    """Only what the replier wrote: drop quoted lines and everything from 'On ... wrote:'."""
    lines = []
    for line in body.splitlines():
        if re.match(r"^\s*On .+wrote:\s*$", line) or line.strip() == "--":
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines).strip()


def imap_reader(config: dict[str, Any]) -> Callable[[], list[Reply]]:
    """Read replies from the team inbox without marking them read (we track what we handled)."""
    import email
    from email import policy

    def fetch() -> list[Reply]:
        since = (datetime.now(timezone.utc) - timedelta(days=config.get("reply_lookback_days", 7)))
        replies = []
        with imaplib.IMAP4_SSL(config["imap_host"]) as imap:
            imap.login(config["sender"], os.environ["OUTREACH_APP_PASSWORD"])
            imap.select("INBOX", readonly=True)
            _, ids = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"), "SUBJECT", '"[ref:"')
            for num in ids[0].split():
                _, data = imap.fetch(num, "(BODY.PEEK[])")
                msg = email.message_from_bytes(data[0][1], policy=policy.default)
                part = msg.get_body(preferencelist=("plain",))
                replies.append(Reply(msg.get("Message-ID", f"no-id-{num.decode()}"), msg.get("From", ""),
                                     msg.get("Subject", ""), part.get_content() if part else ""))
        return replies
    return fetch


CLASSIFY_SCHEMA = {"type": "object", "properties": {"state": {"type": "string", "enum": REPLY_STATES}},
                   "required": ["state"], "additionalProperties": False}
CLASSIFY_SYSTEM = """Classify a sponsor's reply to an event sponsorship email. Pick one state:
replied_question = asks about the event, audience, tiers or prices, nothing else decided yet;
replied_interested = wants to talk or meet, or says yes in principle;
negotiating = proposes or asks for different terms, a discount, a custom package or a counter-offer;
declined = not interested; opted_out = asks not to be contacted again. Return JSON {"state": ...}."""

OPT_OUT_RE = re.compile(r"unsubscribe|opt[\s-]?out|remove me|stop (emailing|contacting|messaging)|"
                        r"do not (contact|email)|don't (contact|email)", re.I)
RULES = [  # fallback when no model is configured; the first match wins
    ("declined", re.compile(r"not interested|no budget|we('ll| will) pass|not this (year|time)|decline", re.I)),
    ("negotiating", re.compile(r"discount|negotiat|counter|lower (price|rate)|custom package|instead of", re.I)),
    ("replied_interested", re.compile(r"interested|let'?s (talk|connect|meet)|schedule|set up a call|"
                                      r"book a (call|meeting)|sounds good|keen", re.I)),
    ("replied_question", re.compile(r"\?")),
]


def classify(text: str, model: ModelCall | None, db: sqlite3.Connection, run_id: str) -> str:
    """Opt-out is decided by code first, so a classification mistake can never email an opt-out."""
    if OPT_OUT_RE.search(text):
        return "opted_out"
    if model:
        out, cost = model(CLASSIFY_SYSTEM, text, CLASSIFY_SCHEMA)
        ledger.record(db, run_id, "anthropic", "messages:classify", "ok" if out else "error", cost_usd=cost)
        state = (_parse_json(out) or {}).get("state")
        if state in REPLY_STATES:
            return state
    for state, pattern in RULES:
        if pattern.search(text):
            return state
    return "needs_person"


ANSWER_SCHEMA = {"type": "object",
                 "properties": {"answerable": {"type": "boolean"}, "answer": {"type": "string"}},
                 "required": ["answerable", "answer"], "additionalProperties": False}
ANSWER_SYSTEM = """You answer a sponsor's question on behalf of an event host, using ONLY the event facts given.
Copy every number exactly as written in the facts. If the facts don't answer the question,
set answerable to false and leave answer empty. Never agree to terms, discounts or custom deals.
Under 120 words. Return JSON {"answerable": ..., "answer": ...}."""


def answer_question(question: str, brief: Brief, model: ModelCall | None,
                    db: sqlite3.Connection, run_id: str) -> tuple[str | None, str]:
    """(answer, reason). None = can't answer from the brief alone: hand to the host."""
    if model is None:
        return None, "no model configured to draft an answer"
    facts = json.dumps(brief_facts(brief), ensure_ascii=False)
    out, cost = model(ANSWER_SYSTEM, json.dumps({"facts": brief_facts(brief), "question": question},
                                                ensure_ascii=False), ANSWER_SCHEMA)
    ledger.record(db, run_id, "anthropic", "messages:answer", "ok" if out else "error", cost_usd=cost)
    parsed = _parse_json(out) or {}
    if not parsed.get("answerable") or not parsed.get("answer"):
        return None, "the brief doesn't answer this question"
    bad = unsupported_numbers(parsed["answer"], [facts])
    if bad:
        return None, f"drafted answer used numbers not in the brief: {sorted(bad)}"
    return parsed["answer"], "answered from the brief's facts"


def make_invite(brief: Brief, run_id: str, company_id: int, company: str, start: datetime,
                config: dict[str, Any]) -> str:
    """A calendar invite (.ics). Organizer and attendee are both the team inbox during the event."""
    fmt = "%Y%m%dT%H%M%SZ"
    start_utc = start.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(minutes=config["meeting_minutes"])
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sponsor-agent//outreach//EN", "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{run_id}-{company_id}@sponsor-agent.local",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start_utc.strftime(fmt)}", f"DTEND:{end_utc.strftime(fmt)}",
        f"SUMMARY:{brief.event_name} sponsorship call with {company}",
        f"ORGANIZER:mailto:{config['team_inbox']}",
        f"ATTENDEE;RSVP=TRUE:mailto:{config['team_inbox']}",
        "END:VEVENT", "END:VCALENDAR", ""])


def next_slot(db: sqlite3.Connection, run_id: str, config: dict[str, Any]) -> datetime | None:
    """Slots are handed out in order, one per booked meeting, so two sponsors never share a slot."""
    booked = db.execute("SELECT COUNT(*) FROM outreach WHERE run_id = ? AND state = 'meeting_booked'",
                        (run_id,)).fetchone()[0]
    slots = config.get("meeting_slots") or []
    return datetime.fromisoformat(slots[booked]) if booked < len(slots) else None


def handle_replies(db: sqlite3.Connection, run_id: str, brief: Brief, config: dict[str, Any],
                   reader: Callable[[], list[Reply]], sender: Callable[[EmailMessage], None],
                   model: ModelCall | None = None,
                   ask: Callable[[str], str] = input, show: Callable[[str], None] = print) -> list[dict]:
    """Read the team inbox, classify each new reply, and take the next step for that company.

    A reply is marked handled only after its step is done, so a crash re-reads it; every
    email it triggers is do_once, so re-reading never sends twice. Its label is saved the
    first time (run_step): a second model call could label it differently and send a
    different email under a different once-only key.
    """
    ensure_tables(db)
    replies = reader()
    ledger.record(db, run_id, "imap", "fetch", "ok")
    own = parseaddr(config["sender"])[1].lower()
    for reply in replies:
        if parseaddr(reply.sender)[1].lower() == own:
            continue                                   # our own outgoing mail in the same inbox
        ref = REF_RE.search(reply.subject)
        if not ref or ref.group(1) != run_id:
            continue
        company_id = int(ref.group(2))
        row = get_row(db, run_id, company_id)
        if row is None or db.execute("SELECT 1 FROM replies_seen WHERE message_id = ?",
                                     (reply.message_id,)).fetchone():
            continue

        text = new_text(reply.body)
        state = run_step(db, run_id, f"classify:{reply.message_id}", lambda: classify(text, model, db, run_id))
        key = hashlib.sha256(reply.message_id.encode()).hexdigest()[:10]
        _act_on_reply(db, run_id, brief, config, sender, model, ask, show, row, reply, text, state, key)
        db.execute("INSERT OR IGNORE INTO replies_seen VALUES (?, ?, ?, ?, ?)",
                   (reply.message_id, run_id, company_id, state, now_iso()))
        db.commit()
    return pipeline(db, run_id)


def _act_on_reply(db, run_id, brief, config, sender, model, ask, show, row, reply, text, state, key) -> None:
    company_id, company = row["company_id"], row["company_name"]
    contact = json.loads(row["contact_json"]) if row["contact_json"] else None
    to_role = f"{contact['title']}, {company}" if contact else f"sponsorship team, {company}"
    hello = f"Hi {contact['name'].split()[0]}," if contact else "Hi,"
    subject = f"Re: Sponsoring {brief.event_name}"

    if state == "opted_out":
        db.execute("INSERT OR IGNORE INTO opt_outs VALUES (?, ?, ?)", (company_id, company, now_iso()))
        db.commit()
        set_state(db, run_id, company_id, "opted_out", "asked not to be contacted: never emailed again")
        return
    if state == "declined":
        set_state(db, run_id, company_id, "declined", "said no")
        return
    if state == "needs_person":
        set_state(db, run_id, company_id, "needs_person", "couldn't classify the reply: a person should read it")
        return
    if state == "negotiating":
        _handoff(db, run_id, config, sender, ask, show, row, reply, text, key,
                 "wants different terms: negotiation is the host's job")
        return

    if state == "replied_question":
        set_state(db, run_id, company_id, "replied_question", "asked a question")
        answer, reason = answer_question(text, brief, model, db, run_id)
        if answer is None:
            _handoff(db, run_id, config, sender, ask, show, row, reply, text, key, reason, keep_state=True)
            return
        draft = Draft(company_id, company, f"answer:{key}", to_role, subject,
                      f"{hello}\n\n{answer}\n\n{brief.host_name}", in_reply_to=reply.message_id,
                      notes=[reason])
        if approve([draft], run_id, config, ask, show) and send_once(db, run_id, draft, config, sender):
            set_state(db, run_id, company_id, "replied_question", "question answered from the brief")
        return

    # replied_interested: book a meeting
    set_state(db, run_id, company_id, "replied_interested", "wants to talk")
    slot = next_slot(db, run_id, config)
    if slot is None and not config.get("booking_link"):
        set_state(db, run_id, company_id, "needs_person", "interested, but no free meeting slot is configured")
        return
    body = f"{hello}\n\nGreat to hear from you."
    attachments = []
    if slot:
        body += f" I've sent a calendar invite for {slot.strftime('%d %b %Y, %H:%M %Z').strip()}."
        attachments.append(("invite.ics", make_invite(brief, run_id, company_id, company, slot, config),
                            "calendar"))
    if config.get("booking_link"):
        body += f" If another time suits you better, pick one here: {config['booking_link']}"
    draft = Draft(company_id, company, "invite", to_role, subject, body + f"\n\n{brief.host_name}",
                  attachments=attachments, in_reply_to=reply.message_id, event_key=event_key(brief))
    if approve([draft], run_id, config, ask, show) and send_once(db, run_id, draft, config, sender):
        set_state(db, run_id, company_id, "meeting_booked",
                  f"meeting invite sent for {slot.isoformat()}" if slot else "booking link sent")


def _handoff(db, run_id, config, sender, ask, show, row, reply, text, key, reason, keep_state=False) -> None:
    """Hand the thread to the host with the sponsor's words. The agent never agrees terms."""
    company_id, company = row["company_id"], row["company_name"]
    draft = Draft(company_id, company, f"handoff:{key}", f"host, re {company}",
                  f"[handoff] {company} needs you", f"{company} replied:\n\n{text}\n\nWhy this needs you: {reason}")
    host_config = {**config, "team_inbox": config.get("host_inbox") or config["team_inbox"]}
    if send_once(db, run_id, draft, host_config, sender):
        if keep_state:
            set_state(db, run_id, company_id, "needs_person", f"handed to the host: {reason}")
        else:
            set_state(db, run_id, company_id, "negotiating", f"handed to the host: {reason}")


# -- model -------------------------------------------------------------------------

def anthropic_caller(config: dict[str, Any]) -> ModelCall:
    """call(system, user, schema) -> (text, cost_usd). Same pattern as brief.py, schema per call."""
    import anthropic

    client = anthropic.Anthropic()
    prices = config.get("price_usd_per_mtok", {})

    def call(system: str, user: str, schema: dict) -> tuple[str, float | None]:
        response = client.beta.messages.create(
            model=config["model"],
            max_tokens=config.get("max_tokens", 2048),
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": config.get("effort", "low"),
                           "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        text = "" if response.stop_reason in ("refusal", "max_tokens") else next(
            (b.text for b in response.content if b.type == "text"), "")
        price = prices.get(response.model) or prices.get(config["model"])
        cost = ((response.usage.input_tokens * price["input"] + response.usage.output_tokens * price["output"])
                / 1_000_000) if price else None
        return text, cost

    return call


def _parse_json(text: str | None) -> dict | None:
    try:
        value = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# -- CLI ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """python outreach.py status|replies|follow-ups RUN_ID BRIEF_JSON

    status: print the pipeline. replies / follow-ups: one pass against the team inbox.
    BRIEF_JSON is a Brief saved with dataclasses.asdict (needed for replies and follow-ups).
    """
    import argparse

    import agent_db
    from records import Tier

    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument("command", choices=["status", "replies", "follow-ups"])
    p.add_argument("run_id")
    p.add_argument("brief_json", nargs="?")
    args = p.parse_args(argv)

    db = agent_db.connect()
    if args.command == "status":
        for r in pipeline(db, args.run_id):
            print(f"{r['company']:<30} {r['state']:<20} {r['contact'] or '-':<35} {r['why']}")
        return
    if not args.brief_json:
        p.error("brief_json is required for replies and follow-ups")
    with open(args.brief_json, encoding="utf-8") as f:
        raw = json.load(f)
    raw["tiers"] = [Tier(**t) for t in raw.get("tiers", [])]
    brief = Brief(**raw)
    config = load_config()
    model = anthropic_caller(config) if config.get("model") else None
    if args.command == "replies":
        rows = handle_replies(db, args.run_id, brief, config, imap_reader(config), smtp_sender(config), model)
    else:
        rows = run_follow_ups(db, args.run_id, brief, config, smtp_sender(config))
    for r in rows:
        print(f"{r['company']:<30} {r['state']:<20} {r['why']}")


if __name__ == "__main__":
    main()
