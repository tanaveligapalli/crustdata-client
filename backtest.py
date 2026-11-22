"""Block 5: back-test. Do past events' real sponsors rank above the companies that didn't sponsor?

Past events come from verified past-sponsor facts (block 3's post pools, read from the
evidence ledger). For each event with enough resolved sponsors:
  - every company in the data is a candidate; the event's own sponsors are the answers;
  - signals are rebuilt with block 3's own merge, from facts dated before the event's
    cutoff only, and never from facts about the event itself;
  - candidates are ranked with block 4's scoring.rank, and we measure how often a real
    sponsor sits above a non-sponsor.

What this can and can't test: at any past cutoff the only signal we can know is
`past_sponsorships` (job facts are from the last 30 days; city facts belong to another
brief's city; the other signals aren't built). So this measures whether past
sponsorships predict the next one, not the weights between signals.

Leakage is the main risk: a post about the same event under another name ("BRICS" vs
"BRICS Conference 2026") would put the answer into the history. Two facts are treated as
the same event if their names match, one name contains the other, or their event dates
overlap. When unsure, a fact is left out of the history: a score that's too low is safer
than a pass mark that's too high.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import scoring
from candidates import Hit, merge
from records import Brief, Evidence

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
POST_SOURCES = {"linkedin_post", "instagram_post"}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["backtest"]


# -- the data -----------------------------------------------------------------


def load_facts(db: sqlite3.Connection, run_ids: list[str]) -> list[tuple[int, Evidence]]:
    """(company ID, fact) for every fact saved about a company in these runs, once each."""
    marks = ",".join("?" * len(run_ids))
    rows = db.execute(
        f"SELECT * FROM evidence WHERE run_id IN ({marks}) AND subject LIKE 'company:%' "
        "ORDER BY saved_at, rowid", run_ids).fetchall()
    seen, out = set(), []
    for r in rows:
        key = (r["subject"], r["source_url"], r["claim"])
        if key in seen:
            continue
        seen.add(key)
        fact = Evidence(r["claim"], json.loads(r["value_json"]), r["source_url"], r["quote"],
                        r["record_date"], r["retrieved_at"], r["source_type"], r["status"], r["missing"])
        out.append((int(r["subject"].split(":", 1)[1]), fact))
    return out


def is_sponsor_fact(fact: Evidence) -> bool:
    return (fact.status == "verified" and fact.source_type in POST_SOURCES
            and isinstance(fact.value, dict) and fact.value.get("crustdata_company_id") is not None)


def event_name_key(name: str | None) -> str:
    """'14th BMR International Conference, 2026' -> 'bmr international conference 2026'."""
    words = re.sub(r"[^\w\s]", " ", (name or "").casefold()).split()
    return " ".join(w for w in words if not re.fullmatch(r"\d+(st|nd|rd|th)", w))


def _contains(a: str, b: str) -> bool:
    return f" {a} " in f" {b} " or f" {b} " in f" {a} "


@dataclass
class PastEvent:
    names: set[str] = field(default_factory=set)       # event_name_key of every name seen
    dates: set[str] = field(default_factory=set)       # event dates written in the posts
    post_dates: list[str] = field(default_factory=list)
    sponsors: dict[int, str] = field(default_factory=dict)  # company ID -> name as written

    @property
    def cutoff(self) -> str:
        """The first day anything about this event was public: history must be older."""
        return min(self.dates | {d[:10] for d in self.post_dates})

    @property
    def label(self) -> str:
        return max(self.names, key=len) if self.names else f"event on {min(self.dates)}"

    def same_as(self, fact: Evidence) -> bool:
        name = event_name_key(fact.value.get("event_name"))
        dates = set(fact.value.get("event_dates") or [])
        return bool(dates & self.dates) or any(name and _contains(name, n) for n in self.names)


def _identifiable(fact: Evidence) -> bool:
    return bool(event_name_key(fact.value.get("event_name")) or fact.value.get("event_dates"))


def past_events(facts: list[tuple[int, Evidence]]) -> list[PastEvent]:
    """Group sponsor facts into events. Facts with no event name and no event date can't be
    placed, so they're neither events nor history."""
    sponsor_facts = sorted(
        ((cid, f) for cid, f in facts if is_sponsor_fact(f) and _identifiable(f)),
        key=lambda x: (x[1].record_date or "", x[1].source_url, x[0]))
    events: list[PastEvent] = []
    for company_id, fact in sponsor_facts:
        event = next((e for e in events if e.same_as(fact)), None)
        if event is None:
            event = PastEvent()
            events.append(event)
        if name := event_name_key(fact.value.get("event_name")):
            event.names.add(name)
        event.dates |= set(fact.value.get("event_dates") or [])
        event.post_dates.append(fact.record_date)
        event.sponsors.setdefault(company_id, fact.value.get("sponsor_name_as_written") or "")
    return events


def history_hits(event: PastEvent, facts: list[tuple[int, Evidence]]) -> list[Hit]:
    """Sponsor facts the agent could have known before this event: older than its cutoff,
    about some other event we can identify."""
    return [Hit(cid, f.value.get("sponsor_name_as_written") or f"company {cid}", "past_sponsor",
                "past_sponsorships", [f])
            for cid, f in facts
            if is_sponsor_fact(f) and _identifiable(f) and not event.same_as(f)
            and f.record_date[:10] < event.cutoff]


# -- one event ----------------------------------------------------------------


def rank_as_of(event: PastEvent, facts: list[tuple[int, Evidence]], universe: dict[int, str],
               profile: str, candidates_cfg: dict[str, Any], scoring_cfg: dict[str, Any]):
    """Every company in the data, scored with only what was known before the event."""
    # No real host: for a post written by a sponsor, the "organizer" is that sponsor, and
    # merge would drop it as the host. This name can't match a company.
    brief = Brief(event_name=event.label, profile=profile, event_date=event.cutoff, city=None,
                  audience_size=None, audience="", host_name="(back-test: no host)", host_website=None)
    hits = [Hit(cid, name, "backtest") for cid, name in universe.items()]
    candidates = merge(hits + history_hits(event, facts), brief, candidates_cfg)
    return scoring.rank(candidates, brief, scoring_cfg)


def measure(ranked, sponsors: set[int], top_k: int) -> dict[str, Any]:
    """Pairwise: a sponsor above a non-sponsor wins, a tie counts half (AUC).
    An unknown score (None) is below every known one, as in scoring.rank."""
    key = {c.company_id: (c.score if c.score is not None else -1.0) for c in ranked}
    pos = [key[c] for c in key if c in sponsors]
    neg = [key[c] for c in key if c not in sponsors]
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    decided = sum(p != n for p in pos for n in neg)
    top = {c.company_id for c in ranked[:top_k]}
    return {
        "sponsors": len(pos), "non_sponsors": len(neg), "pairs": len(pos) * len(neg),
        "wins": wins, "decided": decided,
        "auc": wins / (len(pos) * len(neg)) if pos and neg else None,
        "sponsors_known": sum(p >= 0 for p in pos),
        # With k or fewer candidates every sponsor is in the top k: that says nothing.
        "recall_at_k": len(top & sponsors) / len(pos) if pos and len(ranked) > top_k else None,
    }


# -- the whole back-test ------------------------------------------------------


def run(facts: list[tuple[int, Evidence]], profile: str, cfg: dict[str, Any],
        candidates_cfg: dict[str, Any], scoring_cfg: dict[str, Any]) -> dict[str, Any]:
    universe: dict[int, str] = {}
    for cid, f in facts:
        name = f.value.get("sponsor_name_as_written") if isinstance(f.value, dict) else None
        universe.setdefault(cid, name or f"company {cid}")

    events = past_events(facts)
    tested = [e for e in events if len(e.sponsors) >= cfg["min_sponsors"]]
    rows = []
    for e in tested:
        ranked = rank_as_of(e, facts, universe, profile, candidates_cfg, scoring_cfg)
        rows.append({"event": e.label, "cutoff": e.cutoff,
                     **measure(ranked, set(e.sponsors), cfg["top_k"])})
    rows = [r for r in rows if r["pairs"]]

    pairs = sum(r["pairs"] for r in rows)
    sponsors = sum(r["sponsors"] for r in rows)
    total = {
        "events_found": len(events), "events_tested": len(rows), "sponsors": sponsors,
        "sponsors_known_share": sum(r["sponsors_known"] for r in rows) / sponsors if sponsors else None,
        "decided_share": sum(r["decided"] for r in rows) / pairs if pairs else None,
        "auc": sum(r["wins"] for r in rows) / pairs if pairs else None,
        "mean_recall_at_k": _mean([r["recall_at_k"] for r in rows if r["recall_at_k"] is not None]),
        "top_k": cfg["top_k"],
    }
    missing = []
    if len(rows) < cfg["min_events"]:
        missing.append(f"only {len(rows)} testable events (need {cfg['min_events']}, "
                       f"each with {cfg['min_sponsors']}+ resolved sponsors)")
    if total["decided_share"] is not None and total["decided_share"] < cfg["min_decided_share"]:
        missing.append(f"only {total['decided_share']:.0%} of sponsor/non-sponsor pairs were decided "
                       f"(need {cfg['min_decided_share']:.0%}): most companies have no sponsorship "
                       "history before the events")
    total["status"] = "cannot_verify" if missing else "verified"
    total["missing"] = "; ".join(missing) or None
    return {"events": rows, "total": total}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def report(result: dict[str, Any]) -> str:
    """Markdown for the README. Leads with "cannot verify" when the data can't support a number."""
    t = result["total"]
    lines = ["# Back-test: do past events' real sponsors rank above non-sponsors?", ""]
    if t["status"] == "cannot_verify":
        lines += [f"**Cannot verify.** {t['missing']}.", ""]
    else:
        lines += [f"**A real sponsor ranked above a non-sponsor {t['auc']:.0%} of the time** "
                  f"(AUC; 50% = chance), over {t['events_tested']} past events and {t['sponsors']} sponsors.", ""]

    def pct(x):
        return "-" if x is None else f"{x:.0%}"

    lines += [
        f"- Events found: {t['events_found']}, tested: {t['events_tested']}, sponsors: {t['sponsors']}",
        f"- Sponsors with any known signal before their event: {pct(t['sponsors_known_share'])}",
        f"- Pairs decided (not tied): {pct(t['decided_share'])}",
        f"- AUC: {pct(t['auc'])} · mean recall in top {t['top_k']}: {pct(t['mean_recall_at_k'])}",
        "- Only `past_sponsorships` can be known at a past date, so this tests that signal, not the weights.",
        "", "| Event | Cutoff | Sponsors | Known | Non-sponsors | AUC | Recall@k |", "|---|---|---|---|---|---|---|",
    ]
    for r in result["events"]:
        lines.append(f"| {r['event'].replace('|', '/')} | {r['cutoff']} | {r['sponsors']} | {r['sponsors_known']} | "
                     f"{r['non_sponsors']} | {pct(r['auc'])} | {pct(r['recall_at_k'])} |")
    return "\n".join(lines) + "\n"
