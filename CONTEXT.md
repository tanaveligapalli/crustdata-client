# Team context capsule

Read this before you code. It's written for teammates and for their coding agents
(Claude Code, Codex, Cursor): point your agent at this file first.

---

## 1. What we're building

**ContextCon 05 (BITS Pilani), theme "Services, Replaced":** build an agent that does a whole job a firm is paid for, from request to finished result.

**Our service: a sponsorship agent for medium-scale events in India** (conferences, summits, concerts, music festivals). It replaces the outreach team or sponsorship agency an event host pays today.

**Flow:**
1. **Intake.** The host describes the event and their organization → a structured `Brief`.
2. **Verify the host.** The host organization must be real before any outreach (stops the platform being used to phish sponsors).
3. **Find sponsors.** Three candidate pools, merged by Crustdata company ID:
   past sponsors of similar events · companies hiring sponsorship/partnerships/brand/event-marketing roles in the last 30 days · Crustdata company search. Plus look-alikes of the host's past sponsors.
4. **Score.** Hard filters (duplicates, already a sponsor, category conflicts, restricted categories), then a weighted score. Every input is sourced.
5. **Contacts.** Decision-maker's name, title, business email, only for companies the host selects.
6. **Outreach.** Host approves the campaign → agent sends → reads replies → answers questions from the brief only → books meetings → hands negotiation to the host.
7. **Finished result:** meetings booked with sponsor decision-makers, plus a pipeline showing each company's state and why.

**Two event profiles** (weights live in `config.yaml`, not code):

| Signal | Conference / summit | Concert / festival |
|---|---|---|
| `past_sponsorships` of similar events | 0.30 | 0.30 |
| `audience_fit` | 0.25 | 0.20 |
| `active_hiring` (sponsorship / marketing roles, last 30 days) | 0.15 | 0.20 |
| `budget` (funding, headcount, growth) | 0.15 | 0.15 |
| `city_presence` | 0.10 | 0.10 |
| `reachable_contact` | 0.05 | 0.05 |

---

## 2. Rules that decide our score (from the event handbook)

Judged on: end-to-end execution 25 · facts and verification 20 · robustness 20 · customer value 20 · demo 15.

- **Every fact is an `Evidence` record** with a source URL, the exact quote or field, and the record's date. No facts from model memory.
- **If nothing settles a fact, say "cannot verify" and what's missing.** A wrong fact costs more than "cannot verify". A record with no date is "cannot verify" (enforced in `records.py`).
- **A search snippet is a lead, not a fact.** Fetch the page; the fact must be on it.
- **A person approves anything that can't be undone** (sending an email, booking, payment).
- **Kill the process anytime → restart → nothing happens twice.** Use `steps.py`.
- **Every outside call goes in the cost ledger**, model calls included.
- **During the event our agents only email our own team**, never real companies. Use a team inbox; a teammate plays the sponsor.
- **Personal data:** business contact details only, never personal email/phone. Commenters and reactors are counted, never stored by name. Never contact commenters.
- **Don't use `human_mode`** on web fetch. It made no difference and looks like evading bot protection.
- Repo must be public; commit as you go.

---

## 3. What's in the repo

| File | What it does |
|---|---|
| `crustdata_client.py` | The one Crustdata client. SQLite cache, per-endpoint rate limits, retries, credit limit, ledger rows. |
| `instagram_parser.py` | Parses a fetched Instagram post page: handle, caption, `taken_at`, comment count, edit check. |
| `records.py` | **The shared shapes: `Evidence`, `Brief`, `Tier`, `Signal`, `Candidate`, `Contact`, `OUTREACH_STATES`.** Code against these. |
| `evidence.py` | **The evidence ledger.** Builds `Evidence` from API fields, fetched pages and snippets, and saves/loads every fact (verified or not) per run. |
| `agent_db.py` | Our SQLite tables: ledger, steps, actions, evidence. |
| `ledger.py` | `record(...)` a call, `credits_spent(db, run_id)`. |
| `steps.py` | `run_step` (a finished step never re-runs) and `do_once` (an action happens at most once). |
| `brief.py` | Block 4: host's description → `Brief`. Model extracts, code checks every value against the text. |
| `scoring.py` | Block 4: `score` / `rank` candidates for a `Brief`. Pure code: hard filters, weights from `config.yaml`. |
| `candidates.py` | Block 3: `Brief` → deduplicated `Candidate` list from five pools, with sourced signals. |
| `scripts/capture_fixtures.py` | Saves one real response per endpoint to `tests/fixtures/` (only missing ones). |
| `scripts/instagram_survey.py`, `reports/` | Survey: 15/15 Instagram posts parsed OK. |

### Using the client
```python
import agent_db
from crustdata_client import CrustdataClient, SourceUnavailable

db = agent_db.connect()                      # agent.sqlite3 (gitignored)
client = CrustdataClient(run_id="run-42", ledger_db=db, credit_limit=300,
                         ttl_hours={"default": 24, "/web/search/live": 6})

r = client.call("/job/search", body)         # ApiResponse
r.data, r.retrieved_at, r.from_cache, r.credits
client.request("/job/search", body)          # just the JSON
```
- `SourceUnavailable`: the source kept failing. **Hold the case for a person** (`needs_person`), never treat it as "no results".
- `CrustdataError`: request refused (bad body, plan gate). Fix the request.
- `BudgetExceeded`: the run hit its credit limit. Stop and report.
- `offline=True` serves only from the cache (demo backup); no API key needed.

### Restart safety
```python
from steps import run_step, do_once

candidates = run_step(db, run_id, "find_candidates", lambda: find_candidates(...))
do_once(db, run_id, f"email:{company_id}:intro", "email", lambda: send_email(...))
```
`do_once` raises `ActionNeedsCheck` if an action started but the process died before it finished: show it to a person, don't resend.

### Recording facts (evidence ledger)
Never build an `Evidence` by hand for a fact you found: use `evidence.py`, which applies the rules.
```python
import evidence

r = client.call("/job/search", body)
job = r.data["job_listings"][0]
fact = evidence.from_api("/job/search", r, "job_listings[0].job_details.title", job["job_details"]["title"],
                         claim="Acme is hiring a Head of Partnerships",
                         record_date=job["metadata"]["date_added"], source_url=job["job_details"]["url"])

# A page: verified only if the quote is on the fetched page and there is a record date.
fact = evidence.from_web_fetch(result, r.retrieved_at, quote="Gold Partner: Acme",
                               claim="Acme was Gold Partner at X 2026", value="Gold Partner",
                               record_date="2026-09-04")
# Instagram: from_page(url, post["caption"], quote, ..., evidence.unix_to_iso(post["taken_at"]), ..., "instagram")
# A search snippet is only a lead: evidence.from_snippet(...) is always cannot_verify.
# Nothing settled it: evidence.cannot_verify(claim, "what is missing").

evidence.save(db, run_id, f"company:{company_id}", fact)   # also "host", "event"
evidence.load(db, run_id, f"company:{company_id}")         # every fact, verified or not
```
A record date must be ISO (`2026-09-04` or a full date-time); "2w" or no date → `cannot_verify`.

### Model calls
Use the Anthropic Python SDK. Put the model ID in `config.yaml` (e.g. `claude-sonnet-5-5`). Record every call:
`ledger.record(db, run_id, "anthropic", "messages", "ok", cost_usd=...)`.
The model only **extracts and drafts**. Scoring and decisions are plain code, so the same request worded differently gives the same answer.

---

## 4. What we learned about the data (tested on our account)

- **API key:** env var `CRUSTDATA_API_KEY` (your own key from app.crustdata.com → API Keys). Tests don't need it.
- **Rate limits (per minute):** company/person identify **5**, web search/fetch 10, enrich 15, searches 30. The client enforces them. Batch company identify: up to 25 names per call.
- **LinkedIn post index is ~2 months behind** (newest post seen: 17 Sep 2026). Use it for **history**, not recent news.
  - India 2026: "conference + sponsor" 1,020 posts · "summit + partner" 5,030 · "concert + sponsor" 41 · "music festival + partner" 16. Concerts need web search.
  - `limit: 0` returns `total_count` for free.
- **Job postings are fresh.** Last 30 days: partnerships 5,079 · brand manager 1,629 · sponsorship 796 · event marketing 499. Best "has budget now" signal.
- **Web search** finds Indian event sponsor posts, but snippets have no dates and many URLs are Google redirects. Fetch the page.
- **Instagram:** a plain logged-out fetch works (no login wall). The caption is in the page's meta/JSON, `taken_at` is the exact post time. Example: `https://www.instagram.com/p/Dc3Hy3qjeRC/` (SIDTM Pune Hackathon, 4 Sep 2026: Trainocate Networks India Pvt Ltd, ITC Limited, The 3D Crafted, Nasher Miles, Trend Tree, WOW Momo, KLAW, Campus Times Pune, Maharashtra Times). Small brands probably won't resolve to a company → "cannot verify".
- **Plan gates:** live social search, all batch endpoints, `years_of_experience`, some person fields are off. Naming a gated field in `fields` fails the whole call.
- **Job search:** many postings come from Indeed with `crustdata_company_id = null`. Filter `job_details.source = "professional_network"` (LinkedIn) to get company IDs. India, last 30 days, sponsorship/partnerships/brand roles on LinkedIn: ~90 postings (22 Nov).
- **Post search country:** `actor.location.country` is stored as `"IND"` (`"IN"` and `"India"` return 0). Many company authors have no location, so OR it with `text [.] "India"`.
- **Company search:** `basic_info.industries` values are a closed set: check with company autocomplete. Industries come back under `taxonomy.professional_network_industries` (+0.1 credits/result). Job search returns `company.basic_info.industries` at base price.
- **Filter operators:** `=`, `in`, `=>` (not `>=`), `[.]` exact phrase, `(.)` all words (fuzzy).
- **Costs:** company search 0.03/result, person search 0.03/result (+0.1 if you filter on title), social post 0.5/post, web search 1/query, web fetch 1/page, company identify 0.2/match, business email +1.

---

## 5. Blocks open for teammates

Make your own file(s) so we don't collide. Add your settings as **your own top-level section** in `config.yaml`. Add tests in `tests/` (`unittest`, offline, fakes or fixtures; no live calls in tests).

### Block 3b · Instagram sponsor extraction (suggested: Tanav)
- **File:** `instagram_sponsors.py`
- **In:** a parsed post (`instagram_parser.parse_post`). **Out:** a list of `Evidence` (one per sponsor) and resolved company IDs.
- Model reads the caption → JSON `{event, event_date, city, sponsors: [{name, role}]}`.
- **Code checks every sponsor name appears word-for-word in the caption** (drops invented names).
- Split "A and B" lines. Keep both dates: the post date (`taken_at`) and the event date written in the caption.
- Resolve names with `/company/identify` (`{"names": [...]}`, max 25). No confident match → `cannot_verify`, "no company record matches this brand".
- **Done when:** the SIDTM post above gives 9 sponsors with roles; ITC Limited and Trainocate resolve; small brands come out `cannot_verify`.

### Block 4 · Brief and scoring (done)
- **Files:** `brief.py`, `scoring.py`, `config.yaml` (profiles + weights section).
- `brief.py`: host's free-text description → `Brief` (model extracts, code validates: profile must be `conference` or `concert`, dates ISO, missing fields `None`).
- `scoring.py`: `score(candidate, brief, config) -> Candidate`.
  - Hard filters first; set `excluded_reason`, keep the candidate.
  - **Unknown signals (`value=None`) are left out and the other weights rescaled**; `confidence` = share of weight that was known.
  - Pure code, no model, no API.
- **Done when:** two wordings of the same event give the same `Brief` profile and the same ranking; a candidate with half its signals unknown gets confidence 0.5, not a low score.
- **How to use it (for blocks 3 and 6):**
  - `brief.make_brief(text, db, run_id, config, call_model)` → `(Brief, dropped)`. `BriefIncomplete.missing` lists what to ask the host (then run again with their answers); `ExtractionFailed` means the model gave no usable JSON twice.
  - `scoring.rank(candidates, brief, config)` returns every candidate: excluded ones are kept with `excluded_reason` and ranked last.
  - **A signal counts only if it has a value and at least one verified `Evidence` (with a date).** Without that it's unknown, and a candidate with no known signals gets `score=None`, `confidence=0`.
  - Signal names must be weight keys in `config.yaml` (`scoring.profiles`); anything else raises `ValueError`, as do values outside 0..1.
  - **`Candidate.industries`** (new field, default `[]`): fill it from Crustdata `basic_info.industries`. The category filters read it; empty means no category filter applies.

### Block 6 · Outreach and replies
- **File:** `outreach.py` (+ an `outreach` section in `config.yaml`).
- Team Gmail account with an app password (SMTP to send, IMAP to read). **Every email goes to the team inbox**, subject tagged with the real target: "[to: Head of Partnerships, Acme]".
- **Approval:** a CLI prompt showing every draft; nothing sends without "y".
- Every send is `do_once(db, run_id, f"email:{company_id}:{kind}", "email", ...)`.
- Reply classification → one of `OUTREACH_STATES`. Answer questions only with facts from the `Brief` (tiers, prices, audience). "Interested" → book a meeting (calendar invite `.ics` or a booking link). "Negotiating" → hand to the host.
- Max 2 follow-ups; opt-out line in every email; opted-out companies are never emailed again.
- **Done when:** a teammate replying as the sponsor drives a deal to `meeting_booked`, and killing the process mid-campaign then restarting sends nothing twice.

### Host verification (small)
- **File:** `host_check.py`. `/company/identify` on the host's domain + web fetch of its site → `Evidence`. Not verified → outreach is blocked with the reason.

### README, diagram, business case (no code)
- README must state: the job and who pays today, input, finished result, pass mark, every data source, where a person approves, the cost ledger, approaches we considered.
- Diagram: components, data flow, where Crustdata/web/model calls, approvals, retries and the ledger sit. Excalidraw or Mermaid, link open to anyone.
- Business case: buyer = event organizers/agencies running several medium events a year; price in ₹ (per-event fee + success fee) vs. measured cost per job from the ledger.

### Block 3 · Candidate pools (done, except a live run of the post pools)
- **File:** `candidates.py` (+ the `candidates` section in `config.yaml`).
- **Use:** `candidates.find_candidates(brief, client, db, run_id, candidates.load_config(), instagram_sponsors.load_config(), call_model)` → `(candidates, failed_pools)`.
  - One `Candidate` per Crustdata company ID, sorted by ID, with `found_by`, `domain`, `industries` and `signals`. Feed it straight to `scoring.rank`.
  - `failed_pools` non-empty → a source kept failing: hold the run for a person (`needs_person`), don't treat it as "no candidates".
  - Every fact is saved to the evidence ledger under `company:<id>`. Sponsor brands with no company record are saved as `cannot_verify` under `unresolved`.
  - `call_model=None` skips the two post pools and lists them in `failed_pools`.
- **Pools:**
  - `hiring`: LinkedIn job postings for sponsorship/partnerships/brand roles in the country, last 30 days.
  - `past_sponsor`: sponsors named in LinkedIn posts (`/social_post/search`) and in Instagram posts found by web search. Both go through block 3b's extraction and name matching.
  - `host_past_sponsor`: the brief's `past_sponsors`, resolved to company IDs.
  - `lookalike`: companies in the same industries as those past sponsors.
  - `company_search`: companies in the profile's industries (`config.yaml`) with an office in the event city.
- **Signals it fills:** `past_sponsorships`, `active_hiring`, `city_presence` (a job posting in the event city). Value = verified facts from distinct sources ÷ `signals_full_at` (max 1.0). No verified fact → no value (unknown, never 0).
- **Not filled yet:** `budget`, `audience_fit`, `reachable_contact`. Scoring treats them as unknown, which lowers confidence but not the score.
- **Live run (22 Nov, Bengaluru fintech summit, no model):** 132 candidates for 9.5 credits. The post pools are tested offline only: the Anthropic SDK and key aren't set up on this machine yet.

**Done:** fixtures capture, block 2 (evidence ledger, `evidence.py`), block 3 (candidate pools, `candidates.py`).

---

## 6. Working agreement

- **Keep code as simple as possible and very readable.** Plain functions, short docstrings that say *why*, no speculative features.
- Python 3.11+, standard library first. Read and write files with `encoding="utf-8"` (Windows teammates).
- `git pull --rebase` before you push. Small commits, clear messages.
- Never commit `.env`, API keys, `*.sqlite3`, or raw pages containing commenters' names.
- Run all tests before pushing: `python -m unittest discover -s tests`
