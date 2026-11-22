"""The evidence ledger (block 2): turn sources into Evidence records and keep every one.

The rules this file enforces, so other blocks can't skip them:
- A fact from a page counts only if the fetched page contains the quoted passage.
- A search snippet is a lead, not a fact: it always comes out "cannot verify".
- A fact with no record date is "cannot verify" (checked in records.Evidence).
- Nothing is dropped: "cannot verify" facts are saved too, with what is missing,
  because showing "cannot verify" scores better than a wrong fact or a silent gap.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from agent_db import now_iso
from crustdata_client import BASE_URL, ApiResponse
from records import Evidence

# -- building evidence --------------------------------------------------------


def from_api(endpoint: str, response: ApiResponse, field_path: str, value: Any,
             claim: str, record_date: str | None, source_url: str | None = None) -> Evidence:
    """A fact read from a Crustdata response field.

    source_url: the record's own link if it has one (e.g. the job posting URL);
                otherwise the API endpoint is the source.
    """
    return Evidence(
        claim=claim,
        value=value,
        source_url=source_url or BASE_URL + endpoint,
        quote=f"{field_path} = {json.dumps(value, ensure_ascii=False)}",
        record_date=record_date,
        retrieved_at=response.retrieved_at,
        source_type=f"crustdata:{endpoint}",
    )


def from_page(url: str, page_text: str, quote: str, claim: str, value: Any,
              record_date: str | None, retrieved_at: str, source_type: str = "web_fetch") -> Evidence:
    """A fact stated on a fetched page. It is verified only if `quote` is really on the page.

    page_text: the text the fact comes from: a page's visible text (see `page_text`)
               or an Instagram caption from instagram_parser.parse_post.
    """
    if quote_on_page(quote, page_text):
        status, missing = "verified", None
    else:
        status, missing = "cannot_verify", "the fetched page does not contain the quoted passage"
    return Evidence(claim, value, url, quote, record_date, retrieved_at, source_type, status, missing)


def from_web_fetch(result: dict[str, Any], retrieved_at: str, quote: str, claim: str,
                   value: Any, record_date: str | None) -> Evidence:
    """A fact from one result of a /web/enrich/live response."""
    url = result.get("url") or ""
    if not (result.get("success") and result.get("content")):
        return cannot_verify(claim, "the page could not be fetched", url, "web_fetch", retrieved_at)
    return from_page(url, page_text(result["content"]), quote, claim, value, record_date, retrieved_at)


def from_snippet(url: str, snippet: str, claim: str, value: Any, retrieved_at: str) -> Evidence:
    """A search result snippet. Kept as a lead to fetch, never counted as a fact."""
    return cannot_verify(claim, "search snippet only: fetch the page to verify", url,
                         "web_search", retrieved_at, quote=snippet, value=value)


def cannot_verify(claim: str, missing: str, source_url: str = "", source_type: str = "none",
                  retrieved_at: str | None = None, quote: str = "", value: Any = None) -> Evidence:
    """A claim nothing settled, and what would be needed to settle it."""
    return Evidence(claim, value, source_url, quote, None, retrieved_at or now_iso(),
                    source_type, "cannot_verify", missing)


# -- checking text ------------------------------------------------------------


def page_text(raw_html: str) -> str:
    """The visible text of an HTML page: scripts, styles and tags removed."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def quote_on_page(quote: str, page_text: str) -> bool:
    """True if the quote appears on the page as whole words, ignoring case, spacing and HTML
    entities. Whole words: a host called "Meta" isn't on a page that only says "metadata"."""
    quote = _normalise(quote)
    if not quote:
        return False
    return re.search(r"(?<!\w)" + re.escape(quote) + r"(?!\w)", _normalise(page_text)) is not None


def _normalise(text: str) -> str:
    return " ".join(html.unescape(text).split()).casefold()


def unix_to_iso(seconds: int | None) -> str | None:
    """Unix time (e.g. Instagram's taken_at) as an ISO date-time in UTC."""
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


# -- the ledger ---------------------------------------------------------------


def save(db: sqlite3.Connection, run_id: str, subject: str, facts: Evidence | list[Evidence]) -> None:
    """Store facts about `subject`. Saving the same claim from the same source again
    replaces it, so a restarted run doesn't duplicate facts."""
    if isinstance(facts, Evidence):
        facts = [facts]
    for fact in facts:
        db.execute(
            "INSERT OR REPLACE INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, subject, fact.claim, json.dumps(fact.value, ensure_ascii=False),
             fact.source_url, fact.quote, fact.record_date, fact.retrieved_at,
             fact.source_type, fact.status, fact.missing, now_iso()),
        )
    db.commit()


def load(db: sqlite3.Connection, run_id: str, subject: str | None = None) -> list[Evidence]:
    """Facts for a run, optionally only those about one subject, in the order they were saved."""
    query = "SELECT * FROM evidence WHERE run_id = ?"
    params: list[str] = [run_id]
    if subject is not None:
        query += " AND subject = ?"
        params.append(subject)
    rows = db.execute(query + " ORDER BY saved_at, rowid", params).fetchall()
    return [
        Evidence(r["claim"], json.loads(r["value_json"]), r["source_url"], r["quote"],
                 r["record_date"], r["retrieved_at"], r["source_type"], r["status"], r["missing"])
        for r in rows
    ]
