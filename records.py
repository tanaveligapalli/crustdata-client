"""The shared record shapes every block reads and writes.

Change a shape only after telling the team: other blocks are coded against it.
Use dataclasses.asdict(record) to turn any record into JSON-ready dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Outreach states, in the order a deal usually moves (block 6).
OUTREACH_STATES = [
    "drafted",            # pitch written, waiting for the host's approval
    "approved",           # host approved, not sent yet
    "sent",
    "follow_up_sent",
    "replied_question",   # agent answers from the brief's facts only
    "replied_interested", # agent books a meeting
    "negotiating",        # handed to the host: agent never agrees terms
    "meeting_booked",     # finished: won
    "declined",           # finished: lost
    "opted_out",          # finished: never contact again
    "no_response",        # finished: 2 follow-ups, no reply
    "needs_person",       # stopped: a source failed or an action may have half-happened
]


@dataclass
class Evidence:
    """One sourced fact. Every fact the agent shows or uses must be one of these."""

    claim: str                  # "ITC Limited was Munching Partner at SIDTM Hackathon 2026"
    value: Any                  # the fact itself, e.g. "Munching Partner"
    source_url: str             # the record: page URL, post URL, or API endpoint
    quote: str                  # exact passage, or "field.path = value" for API data
    record_date: str | None     # date of the record itself (post date, job posting date), ISO
    retrieved_at: str           # when we fetched it: ApiResponse.retrieved_at
    source_type: str            # "instagram", "web_fetch", "crustdata:/job/search", ...
    status: str = "verified"    # verified | cannot_verify
    missing: str | None = None  # for cannot_verify: what is missing

    def __post_init__(self):
        # Rule: a record with no date (or a date we can't read) can't verify a fact.
        if self.status != "verified":
            return
        if self.record_date is None:
            self.status = "cannot_verify"
            self.missing = self.missing or "the record has no date"
        elif not _is_iso_date(self.record_date):
            self.status = "cannot_verify"
            self.missing = self.missing or f"the record date {self.record_date!r} is not an ISO date"


def _is_iso_date(text: str) -> bool:
    try:
        datetime.fromisoformat(text)
        return True
    except (TypeError, ValueError):
        return False


@dataclass
class Tier:
    name: str                   # "Title sponsor"
    price_inr: int | None
    benefits: list[str] = field(default_factory=list)


@dataclass
class Brief:
    """The event, structured from the host's description (block 4)."""

    event_name: str
    profile: str                # "conference" or "concert": picks the weights in config.yaml
    event_date: str | None      # ISO date
    city: str | None
    audience_size: int | None
    audience: str               # who attends, in the host's words
    host_name: str
    host_website: str | None
    tiers: list[Tier] = field(default_factory=list)
    current_sponsors: list[str] = field(default_factory=list)
    past_sponsors: list[str] = field(default_factory=list)
    excluded_categories: list[str] = field(default_factory=list)
    host_type: str | None = None  # "university", "ngo", "company" or "community": picks the weights


@dataclass
class Signal:
    """One scoring input for one company (blocks 3, 3b, 4)."""

    name: str                   # a weight key in config.yaml, e.g. "past_sponsorships"
    value: float | None         # 0 to 1; None = unknown, never treated as 0
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Candidate:
    """A possible sponsor. One per Crustdata company ID: that ID is the only key."""

    company_id: int
    name: str
    domain: str | None
    found_by: list[str] = field(default_factory=list)   # pools: "past_sponsor", "hiring", "company_search"
    industries: list[str] = field(default_factory=list) # Crustdata basic_info.industries; scoring's category filters read it
    signals: list[Signal] = field(default_factory=list)
    score: float | None = None
    confidence: float | None = None                     # share of the weight that had known values
    excluded_reason: str | None = None                  # set by hard filters; excluded candidates are kept, not deleted


@dataclass
class Contact:
    """The decision-maker at a sponsor (block 6). Business details only."""

    company_id: int
    name: str
    title: str
    profile_url: str
    business_email: str | None
    evidence: list[Evidence] = field(default_factory=list)


# -- back from JSON (steps.run_step saves records with dataclasses.asdict) ----------


def brief_from_dict(d: dict[str, Any]) -> Brief:
    return Brief(**{**d, "tiers": [Tier(**t) for t in d.get("tiers", [])]})


def candidate_from_dict(d: dict[str, Any]) -> Candidate:
    signals = [Signal(s["name"], s["value"], [Evidence(**e) for e in s["evidence"]]) for s in d.get("signals", [])]
    return Candidate(**{**d, "signals": signals})


def contact_from_dict(d: dict[str, Any]) -> Contact:
    return Contact(**{**d, "evidence": [Evidence(**e) for e in d.get("evidence", [])]})
