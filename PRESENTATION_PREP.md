# Presentation prep: sponsorship agent

Prep notes for presenting the whole project. Judging weights (from CONTEXT.md):
end-to-end 25 · facts and verification 20 · robustness 20 · customer value 20 · demo 15.
Every section below maps to one of these.

---

## 1. The 30-second pitch

> An event host in India needs sponsors. Today they pay an agency or keep a team that finds
> companies, checks them, finds the right person, writes pitches, chases follow-ups and books
> meetings. Our agent does that whole job, from a plain description of the event to **meetings
> booked with sponsor decision-makers**. Every reason it gives has a source and a date, the
> host approves every email, and nothing is ever sent twice, even after a crash.

## 2. Suggested running order (about 10 minutes)

| Min | Section | Show |
|---|---|---|
| 0–1 | Problem and who pays today | "The job in one picture" (`docs/workflow/09-…png`) |
| 1–2 | Input → finished result | Paste `demo/parc_health_camp.txt` into the web screen |
| 2–6 | Live demo | Brief → host check → ranked sponsors with evidence → pick → contact → pitch → approve → send → reply → meeting |
| 6–8 | Technical design | Pipeline + architecture diagrams (sections 3–4 below) |
| 8–9 | Trust: facts, robustness, people | Evidence, scoring, kill-on-stage, guard rails |
| 9–10 | Business case and what's left | Measured cost, pricing, gaps (section 7) |

Backup if the network or API fails: `python web/server.py --offline` (replays cached runs, no
API calls) or the recorded video (Drive link in README), or the Netlify read-only site.

---

## 3. Technical design: the pipeline

```
Event text ──► run_id = sha256(description)
   │
   ① Research (each stage is a steps.run_step: paid once, replayed after restart)
   │   brief.py         Claude extracts fields → code keeps only values found word-for-word
   │   host_check.py    fetch host website, must name the host, else outreach blocked
   │   candidates.py    4–5 pools merged on crustdata_company_id
   │   company_signals  headcount, growth, funding, listing, followers, hiring, city, alumni, CSR
   │   scoring.py       weighted sum from config.yaml (plain code, no model)
   │
   ② Select & draft
   │   👤 host picks companies
   │   find_contact     /person/search, current role holder, only for picks
   │   draft_pitch      Claude draft; every number must be in brief/evidence, else template
   │   👤 host approves every pitch
   │
   ③ Outreach (outreach.py)
       send_once → steps.do_once → SMTP to our TEAM inbox (real target named in subject)
       replies   → IMAP, match [ref:run:company] → opt-out regex → Claude classify
                 → interested: .ics invite 👤 · question: answer from brief 👤
                 → negotiating: hand to host · no reply: ≤ 2 follow-ups, then no_response
```

### Module map (what to say if asked "what does each file do")

| File | Role |
|---|---|
| `agent.py` | Orchestrator; runs stages in order, every paid stage wrapped in `run_step` |
| `brief.py` | Description → structured `Brief`; profile and host type chosen by keywords in config, so two wordings give the same brief |
| `host_check.py` | Anti-phishing: host's own site must name the host |
| `candidates.py` | Candidate pools: job search, LinkedIn post search, Instagram posts, company search / look-alikes, host's past sponsors |
| `instagram_sponsors.py`, `src/extract.py`, `src/match.py` | Instagram posts → sponsor names (must appear in caption text) → company ID (only one clear winner, else "ambiguous") |
| `company_signals.py` | Turns API fields into signal values 0..1 or `None` (unknown) |
| `scoring.py` | Score = Σ weight × signal over known signals; confidence = known weight / total |
| `evidence.py`, `records.py` | Every fact is an `Evidence` record: URL/field, exact quote, record date, retrieved date; missing → "cannot verify" |
| `crustdata_client.py` | One client for all Crustdata calls: SHA-256 response cache, per-endpoint rate limits, retry with backoff on 429/5xx, credit cap, ledger row per call, `SourceUnavailable` on repeated failure |
| `ledger.py` | Cost ledger: credits, model dollars, SMTP/IMAP |
| `steps.py` | `run_step` (stage once) and `do_once` (side effect at most once) |
| `outreach.py` | Contacts, pitches, sending, follow-ups, replies, meetings, opt-outs |
| `backtest.py` | Do real past sponsors rank above others using only facts dated before the event? |
| `web/server.py` + `web/static/` | Standard-library HTTP server + page; `--debug`, `--offline`, `--credit-limit` |
| `app.py` | Older Streamlit screen over the same agent |
| `agent_db.py` | SQLite schema: ledger, steps, actions, evidence, outreach, opt_outs |

### Key design decisions (and the "why")

1. **The model never decides.** Claude only extracts and drafts. Ranking is code + config
   weights, so the same input always gives the same order and every point traces to a fact.
2. **Unknown ≠ zero.** A missing signal earns nothing and lowers confidence; rank by
   score × confidence so a hiring-only company can't beat a sourced past sponsor.
3. **Snippet is a lead, page is the fact.** Search snippets are never evidence; the quote must
   be on the fetched page as whole words, with a date.
4. **Company identity is strict.** `/company/identify` must have one clear winner on
   confidence (margin 0.05, min 1.0) — e.g. IBM returned several records at 1.0, so guessing
   would attach facts to the wrong company.
5. **Weights depend on host type** (university / NGO / company / community). Past
   sponsorships weigh most everywhere (0.35–0.40); NGOs weigh CSR at 0.25; universities get
   alumni at 0.15.
6. **Restart safety by construction.** `run_id` is a hash of the description. `run_step`
   replays finished stages for 0 credits. `do_once` marks an email "started" before sending;
   if the process dies mid-send it stops and asks a person instead of resending.
7. **Failure is loud.** A source that keeps failing raises `SourceUnavailable` and holds the
   run for a person — silence is never read as "no sponsors".

---

## 4. Trust and safety (facts + robustness, 40 points together)

- **Hallucination:** names must be in source text; numbers in pitches must be in the brief or
  evidence, else a fixed template is used.
- **Human gates:** host picks companies, approves every pitch, every reply answer and invite;
  negotiation always goes to the host.
- **No real companies contacted:** every email goes to our team inbox; a teammate plays the sponsor.
- **Spam limits:** verified host only, ≤ 2 follow-ups, opt-out permanent across runs, every
  email carries an opt-out line.
- **Personal data (DPDP Act 2023):** one business contact per picked company (name, title,
  profile link), no personal email/phone, local SQLite, commenters counted never stored.
- **Tests:** 228 offline unit tests pass (`python -m unittest discover -s tests`), including
  same-brief-for-two-wordings and no-resend-after-crash.

---

## 5. Measured costs (from the ledger, 22 Nov 2026)

| Step | Credits | Model $ |
|---|---|---|
| Find candidates (fintech summit, 158 candidates) | ~37 | ~$0.59 |
| Instagram extraction (16 posts) | 6 | $0.14 |
| Contact + pitch | ~0.9 / contact | ~$0.011 / pitch |

Credit cap per search defaults to 150. Reruns hit the cache, so the first run costs the most.

---

## 6. Likely judge questions — short answers

| Q | A |
|---|---|
| How do you know the ranking is good? | Back-test against real past sponsors. Honest answer: first sample (55 posts, 9 testable events) had no prior sponsorships to rank on, so the result is "cannot verify"; it needs denser collection (~0.8 credits per post). |
| Why not let the LLM rank? | Determinism and traceability: same answer every time, every point sourced. |
| What if I kill it mid-run? | Same description → same run_id → finished stages replay, emails never resend; a send that died mid-way is flagged for a person. |
| What if Crustdata is down? | Retry with backoff, then the run holds for a person. Offline replay for the demo. |
| Could someone use this to phish? | Host must be verified on their own site, every email is approved, team inbox only. |
| Why did you build your own outreach instead of your mail-merge tool? | It sends to real recipients on a schedule; the rules needed team-inbox-only, per-email approval, once-only sends and ledger rows. It can become the delivery layer later. |
| What about wrong person? | Must hold the role now; title priority order; excluded titles like "Private Equity". |
| Cost per event? | ~37 credits + ~$0.59 research, ~0.9 credits per contact. Rupee price per credit is still a gap. |
| What signals didn't you use? | Marketing methods (no data source) and userbase (followers stand in). |

---

## 7. The remaining parts — be upfront about these

These are marked **[gap]** in the README. Say them before a judge finds them; it scores better
under "facts and verification".

1. **Ranking quality is unproven.** Back-test result is "cannot verify"; needs a much larger
   post sample.
2. **Agency pricing figure** (retainer / commission for Indian sponsorship agencies) is not
   sourced yet — the "what it replaces" cost comparison is incomplete without it.
3. **Deal size range** (₹1 lakh–₹50 lakh) is an assumption.
4. **Replies and follow-ups haven't run live** against the team Gmail, so their cost row is
   empty. (The demo recorder uses a sandbox inbox.)
5. **Rupee cost of a credit** depends on the Crustdata plan.
6. **Pricing model** (per-event fee + success fee) is a proposal for the team to decide.
7. **Hosting:** Netlify is read-only or tunnels to a laptop; a real deployment needs a server
   for the Python agent and SQLite.
8. **LinkedIn post index lags ~2 months**, partly covered by Instagram and job postings.

What's next, if asked: denser back-test data collection, live reply handling on the team
inbox, source the agency figure, mail-merge tool as delivery layer, hosted server.

---

## 8. Pre-demo checklist

- [ ] `CRUSTDATA_API_KEY`, `ANTHROPIC_API_KEY` set; `OUTREACH_*` set if sending live
- [ ] Run each `demo/*.txt` once online so `--offline` can replay them
- [ ] Start `python web/server.py --debug` (operator view) — or without `--debug` for a host view
- [ ] Use a run whose outreach hasn't been sent (sending is once-only by design)
- [ ] Have the video and Netlify link open as backups
- [ ] `python -m unittest discover -s tests` green
