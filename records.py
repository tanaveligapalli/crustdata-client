"""The shared record shapes every block reads and writes.

Change a shape only after telling the team: other blocks are coded against it.
Use dataclasses.asdict(record) to turn any record into JSON-ready dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
        # Rule: a record with no date can't verify a fact.
        if self.record_date is None and self.status == "verified":
            self.status = "cannot_verify"
            self.missing = self.missing or "the record has no date"


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
