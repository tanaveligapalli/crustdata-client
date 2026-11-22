"""Block 3b: Instagram sponsor extraction. A parsed post in, sourced records out.

  extract (src/extract.py)  caption -> validated sponsor mentions
  match   (src/match.py)    sponsor names -> Crustdata company IDs
  records (this file)       matched mention -> Evidence (a claim);
                            unmatched / ambiguous sponsor, or a sponsor ask -> Lead (no claim)

The claim's date is always the post's taken_at, never anything the model said.
Records are saved to the ig_sponsor_records table; saving twice is a no-op,
so a restart never writes a record twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_db import now_iso
from records import Evidence
from src import extract, match

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
SOURCE_KIND = "instagram_post"


@dataclass
class Lead:
    """Something worth a person's look that isn't a fact yet: no claim is made."""

    kind: str                          # "sponsor" (couldn't resolve) or "sponsor_ask" (event wants sponsors)
    source_url: str
    source_date: str | None            # the post's taken_at, ISO
    event_name: str | None
    organizer_handle: str | None
    sponsor_name_as_written: str | None = None
    normalised_name: str | None = None
    role: str | None = None
    match_status: str | None = None    # ambiguous | unmatched
    confidence_score: float | None = None
    evidence_text: str | None = None
    contact_hint: str | None = None
    reason: str | None = None


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["instagram_sponsors"]


def iso_from_unix(seconds: int | None) -> str | None:
    if not isinstance(seconds, int):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def build_records(post: dict[str, Any], extracted: dict[str, Any], matches: list[dict[str, Any]],
                  fetched_at: str) -> tuple[list[Evidence], list[Lead]]:
    """One Evidence per matched mention; one Lead per unresolved mention and per sponsor ask."""
    source_date = iso_from_unix(post.get("taken_at"))
    can_claim = post.get("parse_status") == "ok" and source_date is not None
    event = extracted["event_name"]
    handle = extracted["organizer_handle"]
    evidence: list[Evidence] = []
    leads: list[Lead] = []

    for mention, m in zip(extracted["sponsor_mentions"], matches):
        if m["match_status"] == "matched" and can_claim:
            event_text = event or f"an event posted by @{handle}"
            evidence.append(Evidence(
                claim=f"{mention['name']} sponsored {event_text} as {mention['role']}",
                value={
                    "sponsor_name_as_written": mention["name"],
                    "role": mention["role"],
                    "crustdata_company_id": m["crustdata_company_id"],
                    "confidence_score": m["confidence_score"],
                    "event_name": event,
                    "event_dates": extracted["event_dates"],
                    "organizer_handle": handle,
                    "date_confidence": "exact",   # taken_at is the exact post time
                },
                source_url=post["url"],
                quote=mention["evidence_span"],
                record_date=source_date,
                retrieved_at=fetched_at,
                source_type=SOURCE_KIND,
            ))
        else:
            reason = {
                "unmatched": "no company record matches this brand",
                "ambiguous": "several companies match closely, or the best match is below the threshold",
            }.get(m["match_status"], "the post has no date")
            leads.append(Lead(
                kind="sponsor", source_url=post["url"], source_date=source_date,
                event_name=event, organizer_handle=handle,
                sponsor_name_as_written=mention["name"], normalised_name=m["normalised_name"],
                role=mention["role"], match_status=m["match_status"],
                confidence_score=m["confidence_score"], evidence_text=mention["evidence_span"],
                reason=reason,
            ))

    if extracted["sponsor_ask"]:
        leads.append(Lead(
            kind="sponsor_ask", source_url=post["url"], source_date=source_date,
            event_name=event, organizer_handle=handle, contact_hint=extracted["contact_hint"],
            reason="the caption asks for sponsors",
        ))
    return evidence, leads


def _ensure_table(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS ig_sponsor_records (
               record_key  TEXT PRIMARY KEY,
               kind        TEXT NOT NULL,   -- evidence | lead
               source_url  TEXT NOT NULL,
               record_json TEXT NOT NULL,
               created_at  TEXT NOT NULL)"""
    )


def _key(post: dict[str, Any], kind: str, name: str | None, role: str | None) -> str:
    return f"{post['shortcode']}:{kind}:{(name or '').lower()}:{role or ''}"


def save(db: sqlite3.Connection, post: dict[str, Any], evidence: list[Evidence], leads: list[Lead]) -> list[str]:
    """Save records once. Returns the keys of the Evidence rows now stored."""
    _ensure_table(db)
    saved = []
    for e in evidence:
        key = _key(post, "evidence", e.value["sponsor_name_as_written"], e.value["role"])
        db.execute("INSERT OR IGNORE INTO ig_sponsor_records VALUES (?, 'evidence', ?, ?, ?)",
                   (key, e.source_url, json.dumps(asdict(e)), now_iso()))
        saved.append(key)
    for lead in leads:
        key = _key(post, f"lead_{lead.kind}", lead.sponsor_name_as_written, lead.role)
        db.execute("INSERT OR IGNORE INTO ig_sponsor_records VALUES (?, 'lead', ?, ?, ?)",
                   (key, lead.source_url, json.dumps(asdict(lead)), now_iso()))
    db.commit()
    return saved


def process_post(post: dict[str, Any], fetched_at: str, db: sqlite3.Connection, run_id: str,
                 client: Any, config: dict[str, Any],
                 call_model: Callable[[str, str], tuple[str, float | None]]) -> dict[str, Any]:
    """Run all of block 3b on one parsed post (instagram_parser.parse_post output plus `url`)."""
    extracted = extract.extract(post, db, run_id, call_model)
    names = [m["name"] for m in extracted["sponsor_mentions"]]
    matches = match.match_names(names, client, config) if names else []
    evidence, leads = build_records(post, extracted, matches, fetched_at)
    saved = save(db, post, evidence, leads)
    return {"post": post, "extracted": extracted, "matches": matches,
            "evidence": evidence, "leads": leads, "saved_evidence_keys": saved}
