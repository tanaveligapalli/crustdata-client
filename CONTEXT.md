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
3. **Find sponsors.** Five candidate pools, merged by Crustdata company ID:
   past sponsors of similar events (LinkedIn and Instagram posts) · companies hiring sponsorship/partnerships/brand/event-marketing roles in the last 30 days · the host's own past sponsors · their look-alikes · Crustdata company search.
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
| `instagram_sponsors.py`, `src/extract.py`, `src/match.py` | Block 3b: post text → sponsor names (model extracts, code checks) → company IDs → `Evidence`. Also used by block 3 for LinkedIn posts. |
| `host_check.py` | Host verification: the host's own website must name the host, or outreach is blocked. |
| `outreach.py` | Block 6: contact, pitch, approval, send once, read replies, book meetings, follow-ups, pipeline. |
| `scripts/capture_fixtures.py` | Saves one real response per endpoint to `tests/fixtures/` (only missing ones). |
| `scripts/instagram_survey.py`, `reports/` | Survey: 15/15 Instagram posts parsed OK. |
| `scripts/instagram_sponsor_summary.py` | Runs block 3b on every cached Instagram post and prints what resolved. |
| `agent.py` | Block 8: the whole job, stage by stage (`run_step` each), replayable offline. Also the `outbox/` sender when Gmail isn't set up. |
| `app.py`, `.streamlit/config.toml`, `demo/` | Block 8: the Streamlit screen (`streamlit run app.py`), its minimal theme, and the demo event descriptions. |
| `backtest.py`, `scripts/backtest.py` | Block 5: do past events' real sponsors rank above non-sponsors, using only facts from before each event? |

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
- **Python env:** on Anaconda, model calls crash in the SDK's HTTP layer (`Decompressor.decompress() got an unexpected keyword argument 'output_buffer_limit'`: the base env's old `brotlicffi`). Use a fresh venv: `python3 -m venv .venv && .venv/bin/pip install anthropic pyyaml` (`.venv/` is gitignored).
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
- **Post IDs:** a company author's `actor.crustdata_entity_id` and the post's `mentions.company_ids` are Crustdata company IDs (checked: LanceSoft India 1266602, Sunalco Alloys 64995777). Request `mentions` in `fields`.
- **Person endpoints (account check, 22 Nov):** `/person/search`, `/person/enrich`, `/person/identify` are enabled; `/person/contact/enrich` and all batch endpoints are **disabled**. `/person/enrich` has a business-email add-on (+1 credit) that might fill `Contact.business_email`: untested.
- **Filter operators:** `=`, `in`, `=>` (not `>=`), `[.]` exact phrase, `(.)` all words (fuzzy).
- **Costs:** company search 0.03/result, person search 0.03/result (+0.1 if you filter on title), social post 0.5/post, web search 1/query, web fetch 1/page, company identify 0.2/match, business email +1.

---

## 5. Blocks: status and how to use them

Make your own file(s) so we don't collide. Add your settings as **your own top-level section** in `config.yaml`. Add tests in `tests/` (`unittest`, offline, fakes or fixtures; no live calls in tests).

| Block | Status (22 Nov, 17:00; "live" = checked against the real APIs) | Files |
|---|---|---|
| 1 · Skeleton, cache, ledger, restart safety | Done, live (offline replay keeps the original date) | `crustdata_client.py`, `agent_db.py`, `ledger.py`, `steps.py` |
| 2 · Evidence ledger | Done, used live by blocks 3, 3b, host check | `evidence.py` |
| 3 · Candidate pools | Done, run live | `candidates.py` |
| 3b · Instagram sponsor extraction | Done, live on the SIDTM post (2 matching fixes, see below) | `instagram_sponsors.py`, `src/` |
| 4 · Brief and scoring | Done, live (two wordings → same brief and ranking); **2 open issues below** | `brief.py`, `scoring.py` |
| Host verification | Done, live (crash on a domain-less match fixed) | `host_check.py` |
| 6 · Outreach and replies | Contacts live; approval fix; **email not run live: no Gmail env vars yet** | `outreach.py` |
| 8 · End-to-end run and frontend | Done, live through sending (to `outbox/`); offline replay works | `agent.py`, `app.py` |
| Signals `budget`, `audience_fit`, `reachable_contact` | **Open** | — |
| 5 · Back-test | Built; trial run (55 posts) = **cannot verify**: no repeat sponsors in the sample | `backtest.py`, `scripts/backtest.py` |
| 7 · Hard cases + kill test | Partly: "contact who has left" now tested; layoffs needs `budget`; kill test needs the end-to-end run | — |
| README, diagram, business case, video | README and diagram written (gaps marked **[gap]**: agency fee, credit price, replies cost); video open | `README.md` |

### Block 3 · Candidate pools (done)
- **File:** `candidates.py` (+ the `candidates` section in `config.yaml`).
- **Use:** `candidates.find_candidates(brief, client, db, run_id, candidates.load_config(), instagram_sponsors.load_config(), call_model)` → `(candidates, failed_pools)`.
  - One `Candidate` per Crustdata company ID, sorted by ID, with `found_by`, `domain`, `industries` and `signals`. Feed it straight to `scoring.rank`.
  - `failed_pools` non-empty → a source kept failing: hold the run for a person (`needs_person`), don't treat it as "no candidates".
  - Every fact is saved to the evidence ledger under `company:<id>`. Sponsor brands with no company record are saved as `cannot_verify` under `unresolved`.
  - `call_model=None` skips the two post pools and lists them in `failed_pools`.
- **Pools:**
  - `hiring`: LinkedIn job postings for sponsorship/partnerships/brand roles in the country, last 30 days.
  - `past_sponsor`: sponsors named in LinkedIn posts (`/social_post/search`) and in Instagram posts found by web search. Both go through block 3b's extraction (the model reads, code checks each name is in the text).
    - Name → company: on LinkedIn, a name that matches exactly one company **the post tags, or the post's author**, takes that company's ID (`matched_by: "tagged in the post"`). Other names go through block 3b's `/company/identify` matching once per pool, not once per post.
    - Roles in `not_sponsor_roles` (venue, media) don't count: they rarely pay.
    - All posts by one organizer count as **one** sponsorship. Organizers repost the same sponsor list (speaker spotlights, countdowns); counting each post would inflate the signal.
  - `host_past_sponsor`: the brief's `past_sponsors`, resolved to company IDs.
  - `lookalike`: companies in the same industries as those past sponsors.
  - `company_search`: companies in the profile's industries (`config.yaml`) with an office in the event city.
- **Signals it fills:** `past_sponsorships`, `active_hiring`, `city_presence` (a job posting in the event city). Value = verified facts from distinct sources ÷ `signals_full_at` (max 1.0). No verified fact → no value (unknown, never 0).
- **Not filled yet:** `budget`, `audience_fit`, `reachable_contact`. Scoring treats them as unknown, which lowers confidence but not the score.
- **Live runs (22 Nov):**
  - Bengaluru fintech summit, all pools: 158 candidates. The first run of the post pools cost ~37 credits (LinkedIn posts 20, identify 7, company search 2, web search/fetch ~8) and ~$0.59 of model calls; reruns hit the caches.
  - Conference posts: 20 LinkedIn posts → 26 sponsor companies (Smart Meter Summit, ISODA United Summit, NABH, BMR...), 24 of them resolved by post tags. 4 Instagram posts → 1 (Hashout Technologies).
  - Concert posts are noisy: 20 LinkedIn posts → 7 companies, some not Indian events (IMMA Dublin). 3 Instagram posts → 0. As expected from the post counts in section 4.
  - 14 brands came out `cannot_verify` (e.g. LanceSoft: the post tags both LanceSoft India and LanceSoft, Inc., so we don't pick one).
- **For block 4 (scoring):** a company with one strong known signal outranks one with several medium ones, because unknown weights are rescaled away. In the live run, companies with only `active_hiring` + `city_presence` (confidence 0.25) took the top spots above sourced past sponsors. Consider weighting the score by confidence, or shrinking low-confidence scores.

### Block 3b · Instagram sponsor extraction (done)
- **File:** `instagram_sponsors.py`
- **In:** a parsed post (`instagram_parser.parse_post`). **Out:** a list of `Evidence` (one per sponsor) and resolved company IDs.
- Model reads the caption → JSON `{event, event_date, city, sponsors: [{name, role}]}`.
- **Code checks every sponsor name appears word-for-word in the caption** (drops invented names).
- Split "A and B" lines. Keep both dates: the post date (`taken_at`) and the event date written in the caption.
- Resolve names with `/company/identify` (`{"names": [...]}`, max 25). No confident match → `cannot_verify`, "no company record matches this brand".
- **Done when:** the SIDTM post above gives 9 sponsors with roles; ITC Limited and Trainocate resolve; small brands come out `cannot_verify`.
- **Use:** `instagram_sponsors.process_post(post, fetched_at, db, run_id, client, instagram_sponsors.load_config(), extract.anthropic_caller(config))` → `{"evidence": [...], "leads": [...], ...}`.
  - Matching (`src/match.py`) takes a company only when one clearly wins (`min_confidence` **1.0**, `ambiguity_margin` 0.05); otherwise the name becomes a `Lead`, never a guess.
    - Why 1.0: live, identify scores 1.0 for the same name and 0.8 for overlapping words. At 0.8, "Maharashtra Times" matched "Times of Maharashtra News", a different paper: a wrong fact.
    - Same name twice, only one with a website → that one ("ITC Limited" itcportal.com vs a second "ITC Limited" with no site). Different names are never split this way.
- **Live check (22 Nov), SIDTM post:** 9 sponsors with roles. ITC Limited, Nasher Miles, WOW Momo, The 3D Crafted, Trend Tree resolve. Trainocate does **not**: identify only offers "Trainocate India" at 0.6, so it stays `cannot_verify`. KLAW, Campus Times Pune, Maharashtra Times → `cannot_verify`.
  - Extractions are cached per caption, so reruns cost no model calls.
  - Block 3 reuses `extract.extract`, `match.match_names` and `build_records` for LinkedIn posts too.

### Block 4 · Brief and scoring (done, 2 open issues)
- **Live check (22 Nov):** two wordings of the same summit gave the same profile, city, date, size, tiers and sponsors, and the same ranking (excluded categories came back in a different order only).
- **Open issue 1: excluded categories that aren't industries do nothing.** "competing payment companies" becomes the key "payment", but Crustdata industries say "Financial Services", so no payments company is excluded and nobody is told. Either flag unmatched categories for a person, or match them against something richer than industries.
- **Open issue 2: ranking.** One strong known signal beats several medium ones (see block 3's notes).
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
- **Built (`outreach.py`, `tests/test_outreach.py`).** Set env `OUTREACH_SENDER`, `OUTREACH_TEAM_INBOX`, `OUTREACH_APP_PASSWORD`, then:
  ```python
  contact = outreach.find_contact(client, candidate, cfg)          # /person/search; business email not on our plan
  outreach.start_campaign(db, run_id, brief, [(candidate, contact)], cfg,
                          outreach.smtp_sender(cfg), model=outreach.anthropic_caller(cfg), host_check=host_evidence)
  ```
  `python outreach.py replies RUN brief.json` · `python outreach.py follow-ups RUN brief.json` · `python outreach.py status RUN`.
  Outreach refuses to start until `host_check` is verified `Evidence`.
  - Contacts: `find_contact` picks the best title by the `contact_roles` order and skips `contact_exclude_titles`. `business_email` stays `None` (see section 4: `/person/enrich` with the email add-on is untested).
  - The once-only send key is `email:{event}:{company_id}:{kind}`, so even a new run ID can't email the same company twice for the same event.
  - Every drafted pitch is stored (`pitch_drafts`) and reused: the text shown for approval, after a reload or after a crash between approval and sending, is the text that is sent. Before this, a fresh model redraft could go out without approval.
  - **Live check (22 Nov):** `find_contact` works (Navi → "Head - Strategic Partnerships & Fund Raise", Diageo → "Brand Manager Guinness"). The Navi pick is a fundraising title: consider adding "fund" to `contact_exclude_titles`.
  - **Still to do:** set `OUTREACH_SENDER`, `OUTREACH_TEAM_INBOX`, `OUTREACH_APP_PASSWORD` and run it live with a teammate playing the sponsor, to tick "done when".

### Host verification (done)
- **File:** `host_check.py`. `host_check.check_host(brief, client, db, run_id)` → `Evidence`, saved under `host:<name>`.
- Verified only if the host's own website, fetched live, names the host word for word. No website, a dead site or a site that doesn't name the host → `cannot_verify` with the reason, and `outreach.start_campaign` refuses to send.
- If Crustdata knows the domain, its company ID rides along in the evidence value; it isn't required (small clubs often aren't in Crustdata).
- Limit: it doesn't prove the host controls the site. A person still approves every email.
- **Live check (22 Nov):** Razorpay with razorpay.com → verified. A made-up host on example.com → `cannot_verify` ("does not mention ..."); before the fix this crashed because identify returned a match with no domain.

### Block 8 · End-to-end run and frontend (done)
- **Run it:** `pip install -r requirements.txt`, set `CRUSTDATA_API_KEY` and `ANTHROPIC_API_KEY`, then `streamlit run app.py`. Pick an example from `demo/` or type a description, then Run.
- **Screen (`app.py`):** brief → host check → ranked sponsors (score, confidence, why, how found) with each company's evidence → tick companies → contacts and pitches → read and approve each pitch → send → pipeline → cost of the job. It only shows things and collects choices; `agent.py` does the work.
- **Stages (`agent.py`):** `make_brief`, `check_host`, `find_candidates` (ranked), `find_contact`, `draft_pitches`, `send_approved`, `pipeline`, `cost`.
  - The run ID is a hash of the description, and each paid stage is a `steps.run_step`. The same description after a kill replays finished stages from the database, so nothing is paid for or sent twice.
  - **Offline demo:** the "Replay from cache" switch opens the client with `offline=True` and blocks model calls (`agent.NotCached`). A description that ran online once replays fully with no API key.
  - A failed source shows a warning and needs a person to tick "reviewed" before continuing. An unverified host blocks pitches.
- **Pitches:** `draft_pitches` runs `outreach.start_campaign` with every approval answered "no", so it only drafts. Drafts are stored (`pitch_drafts`), so the text the host reads is the text `send_approved` sends.
- **Email:** without `OUTREACH_*` env vars, approved emails are written to `outbox/<run>/NNN.eml` (gitignored) and the screen says so. With them, the team Gmail sends them.
- **Replies stay in the terminal:** `python outreach.py replies RUN_ID brief.json` (the screen offers `brief.json` as a download). There a person approves each answer or invite before it is sent.
- **Live run (22 Nov, `demo/fintech_summit.txt`):** 90 candidates, 2 contacts, 2 model-drafted pitches, 1 approved and written to the outbox. 6.81 credits and $0.03 for this run, because most searches were already cached from earlier runs.
- **`records.py`** now has `brief_from_dict`, `candidate_from_dict`, `contact_from_dict` for records saved with `asdict`.

### Open: the other three signals
- `budget`: headcount, headcount growth and funding from `/company/search` (`headcount`, `funding` groups, +0.2 credits/result each). Negative growth (layoffs) should lower it (a hard case).
- `audience_fit`: compare the brief's audience with who the company sells to. It must stay decided by code and sourced, or be left unknown.
- `reachable_contact`: 1.0 when `outreach.find_contact` finds a sourced contact, unknown otherwise.

### Block 5 · Back-test (built; first result: cannot verify)
- **Run:** `python scripts/backtest.py RUN_ID [RUN_ID ...] [--profile conference] [--out reports/backtest.md]` reads past-sponsor facts already in the evidence ledger (no API or model calls). `--collect 2025-11:2026-09 --posts-per-month N --credit-limit C` first collects LinkedIn sponsor posts month by month with block 3's own post pool (`linkedin_sponsor_pool(..., until=)`), one restart-safe step per month, into the run `backtest-<profile>`.
- **How:** sponsor facts are grouped into past events. For each event with 2+ resolved sponsors, the candidates are its sponsors plus **the sponsors of other events at or after its cutoff**; signals are rebuilt with block 3's `merge` from facts dated **before the cutoff** (the earlier of its stated date and its first post); `scoring.rank` orders them. Measured: AUC over every sponsor/non-sponsor pair (ties half, 50% = chance) and recall in the top k (tied groups count by their share of the places).
  - Why that pool: a company is in the data only because a post names it. One whose only post is older than the event would have "history" just for being there, while real sponsors only have history if they sponsored twice. A trial gave "AUC 0%" that way; it was the method, not the data. With this pool both sides need a repeat, so no repeats = all ties = 50%.
  - Limit: it compares repeat sponsors with companies that sponsor other, later events, not with companies that never sponsor (that needs a timeline-free list, e.g. company search, at extra cost).
- **Leakage guard:** a fact about the same event under another name ("BRICS" vs "BRICS Conference 2026"), or with overlapping event dates, never counts as history. Names are compared without generic words ("summit", "annual"), ordinals and years; a different year is a different edition, so last year's edition does count.
- **What it tests:** only `past_sponsorships` can be known at a past date, so it answers "do past sponsorships predict the next one?", **not** how to weight signals.
- **"Cannot verify" floors** (`backtest` in `config.yaml`): fewer than 5 testable events, under 20% of pairs decided, or fewer than 10 real sponsors with an earlier sponsorship (added after the trial below).
- **Trial (22 Nov, conference, 5 posts/month Nov 2025-Sep 2026, 55 posts, 43.7 credits + $0.73):** 19 events, 9 testable, 110 real sponsors, **0** with an earlier sponsorship in the sample -> **cannot verify** (`reports/backtest.md`). 5 posts/month is ~0.5% of the ~1,000 matching posts a month, so repeats are invisible. A pass mark needs far denser collection (cost scales with posts: ~0.8 credits and ~$0.013 per post).

### Review of blocks 1-7 (22 Nov): fixed, and open decisions
- **Fixed** (each with a test that fails on the old code, except the last, a config change): back-test pool and AUC (above) · `merge` kept a job posting's empty industries, so host-excluded categories didn't apply · `find_contact` used the headline title / first job, so it could pick someone who left or whose title is at another company; titles now match whole words (intern, trainee excluded; "CMO" isn't "CMOS") · a reply re-read after a crash could be labelled differently and send a second email · `host_check` replaced a "no date" reason with "does not mention the host" · `quote_on_page` matched inside words ("Meta" in "metadata") · cities with two names (Bangalore/Bengaluru, Gurgaon/Gurugram...) never matched · hiring keyword "partnerships" dropped (config only, no test): it alone brought 71 of 106 postings (free counts), and in the captured sample 5 of the 6 it alone matched were business development, not sponsorship.
- **Open decisions (team):**
  - Ranking: one job posting in the city (score ~0.6, confidence 0.25) still outranks a sourced past sponsor (0.5). Option: order by score × confidence (evidence first), keep displaying score and confidence. Not changed: it's a product call.
  - The host's own past sponsors (brief) get no signal, so they rank below every scored company. Option: a `past_sponsorships` fact sourced to the brief (`host_brief`).
  - Host check proves the site names the host, not that the host controls the site (someone could name a real company and its site). Option: a one-time code on the site; or state the limit in the README.
  - Accepted trade-off, not a bug: opt-out detection is deliberately broad ("opt out of the gold tier" counts as opt-out).
  - Meeting slots in `config.yaml` are fixed dates (24-25 Nov); past slots aren't skipped. Update them before any later demo.

### Open: hard cases (block 7)
- Still without a test: a company in the middle of layoffs (needs the `budget` signal first), a full kill-and-restart of the end-to-end run (needs the end-to-end run). Covered: a contact who has left or whose matching title is at another company (`outreach.find_contact`, `tests/test_outreach.py::ContactEdgeCases`), two names for one company (block 3 merge), an existing sponsor and category conflicts (scoring), logo-only posts (3b), a source timeout (client + `failed_pools`), a reply re-read after a crash (labelled once, `run_step`).

### README, diagram, business case (no code)
- README must state: the job and who pays today, input, finished result, pass mark, every data source, where a person approves, the cost ledger, approaches we considered.
- Diagram: components, data flow, where Crustdata/web/model calls, approvals, retries and the ledger sit. Excalidraw or Mermaid, link open to anyone.
- Business case: buyer = event organizers/agencies running several medium events a year; price in ₹ (per-event fee + success fee) vs. measured cost per job from the ledger.

---

## 6. Working agreement

- **Keep code as simple as possible and very readable.** Plain functions, short docstrings that say *why*, no speculative features.
- Python 3.11+, standard library first. Read and write files with `encoding="utf-8"` (Windows teammates).
- `git pull --rebase` before you push. Small commits, clear messages.
- Never commit `.env`, API keys, `*.sqlite3`, or raw pages containing commenters' names.
- Run all tests before pushing: `python -m unittest discover -s tests`
