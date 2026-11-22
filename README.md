# Sponsorship agent for medium-scale events in India

ContextCon 05 (BITS Pilani), theme "Services, Replaced". An agent that does the job an
event's sponsorship team or agency is paid for: from the host's description of the event
to meetings booked with sponsor decision-makers.

> **Draft.** Sections marked _to write_ are still open (see CONTEXT.md §5). Numbers marked
> **[gap]** need a sourced figure before submission; don't fill them from memory.

## The job and who pays for it today
_To write._

## Input
_To write._

## Finished result
_To write._

## Pass mark
_To write: the back-test result (CONTEXT.md, "next steps")._

## Data sources
_To write._

## Where a person approves
_To write._

## The cost ledger
_To write._

---

## Approaches we considered

Each row is a choice we made while building, what we tried first, and why we changed it.

| Area | Tried or considered | What we do instead | Why |
|---|---|---|---|
| Sending email | **Our own mail-merge tool** (below) as the agent's outreach engine | `outreach.py`: one draft per company, a person types "y" to send, every send through `steps.do_once` | The tool sends to real recipients on a schedule. The hackathon rules need every email to go to our own team inbox, a person to approve each send, sends that never repeat after a crash, and every call in our cost ledger. |
| Who decides | The model ranks sponsors and judges fit | The model only extracts and drafts; scoring and every decision are plain code (`scoring.py`) | The same request worded differently must give the same answer, and every score input must trace to a sourced fact. |
| Facts | Use search snippets as facts | A snippet is a lead; the fact must be on the fetched page (`evidence.py`) | Snippets have no dates and are often cut off. A wrong fact costs more than "cannot verify". |
| Instagram pages | `human_mode` web fetch | Plain fetch | A plain logged-out fetch returned the same post page, and `human_mode` looks like evading bot protection. |
| Finding Instagram posts | Resolving Google redirect links in web search results | Only use direct `instagram.com/p/` and `/reel/` results (`site` set to a post path) | After ~150 lookups Google answered with a challenge page. We stopped rather than work around it. |
| Sponsor names | Trust the model's list of sponsors | Keep a name only if it appears word-for-word in the caption text (not just a hashtag or @handle), with an exact quote (`src/extract.py`) | Stops invented or guessed sponsors. |
| Matching names to companies | Take the top company identify result | Take it only if one company clearly wins on `confidence_score`; otherwise "ambiguous", no company ID (`src/match.py`) | Many brands share a name: in our run, IBM, POSTULATE and Cyber Dojo each returned two or more records at score 1.0. Guessing would attach facts to the wrong company. |
| Past sponsors | LinkedIn posts alone | LinkedIn posts for history, plus Instagram and job postings for recent activity | The LinkedIn post index runs about two months behind. |

### Our existing mail-merge tool

Our sponsorship team already runs its outreach on a Gmail mail-merge tool we built: a
Chrome extension plus a small backend that sends campaigns from each member's Gmail,
sends follow-ups in the same thread and detects replies.

We didn't use it as the agent's outreach engine for this event, for the reasons in the
table. It isn't wasted: it shows the job is real and done by hand today (see the business
case), and after the event it could become the delivery layer, with `outreach.py`
deciding what to send and the tool delivering only approved emails in the same thread.

---

## Business case

### Buyer
Event organizers and agencies that run several medium-scale events a year in India
(conferences, summits, college fests and hackathons, concerts, music festivals), who
today pay a sponsorship agency or keep an outreach team.

### What it replaces
Finding companies likely to sponsor, checking each one, finding the right person, writing
and sending pitches, chasing follow-ups, answering questions and booking meetings.

- **Done by hand today:** our own team runs its sponsorship outreach on the mail-merge
  tool above. People build every recipient list and pitch template by hand.
- **What agencies charge:** **[gap]** a sourced retainer or commission figure for Indian
  event sponsorship agencies.

### What one job costs us (measured)
From the cost ledger, 22 Nov 2026 runs:

| Step | Run | Crustdata credits | Model cost |
|---|---|---|---|
| Find candidates, all pools (block 3) | Bengaluru fintech summit: 158 candidates | ~37 | ~$0.59 |
| Instagram sponsor extraction (block 3b) | 16 posts, 15 sponsor mentions | 6 | $0.14 |
| Contacts, outreach, replies (blocks 5–6) | **[gap]** not run live yet | **[gap]** | **[gap]** |

- Reruns hit the caches, so the first run of an event costs the most.
- A credit's price in rupees: **[gap]**, from our Crustdata plan. Until then, cost per job is
  stated in credits plus model dollars.

### Price (proposal, for the team to decide)
- **Per-event fee:** covers the run above for one event, whether or not a sponsor signs.
- **Success fee:** a share of each sponsorship closed from a meeting the agent booked.
- Set both against the agency figure **[gap]** and the measured cost per job once the
  outreach step has run live. The margin should come from replacing hours of hand-built
  lists and follow-ups, not from cutting data costs.

---

## Diagram
_To write: components, data flow, where Crustdata, web and model calls happen,
approvals, retries and the ledger._
