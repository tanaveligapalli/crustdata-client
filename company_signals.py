"""Scoring signals about the company itself: size, growth, funding, listing, social presence,
alumni of a university host, and published CSR work. Each value is plain code over a
sourced fact; a fact we can't source leaves the signal unknown, never zero.

  company_facts  one /company/search call per 100 companies (~0.63 credits each):
                 company_size, growth, funding, listed, social_presence
  alumni         one /person/search count per company (~0.23 credits): people who studied
                 at the host university and work there now. University hosts only.
  csr            the company's own homepage -> its CSR link -> a CSR page that names a
                 financial year (~2 credits). Search results for CSR reports are mostly
                 redirect links or PDFs that can't be read (checked live, 22 Nov), so we
                 follow the company's own site instead. Only for the top companies.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

import evidence
from crustdata_client import ApiResponse
from records import Candidate, Evidence, Signal

COMPANY_FIELDS = ["crustdata_company_id", "metadata.updated_at", "basic_info.name",
                  "basic_info.professional_network_url", "basic_info.markets", "headcount.total",
                  "headcount.growth_percent", "funding.last_fundraise_date", "funding.last_round_type",
                  "followers.count", "revenue.public_markets"]

CSR_LINK = re.compile(r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.{0,200}?)</a>""", re.IGNORECASE | re.DOTALL)
CSR_WORDS = re.compile(r"\bCSR\b|corporate[\s-]social[\s-]responsibility", re.IGNORECASE)
# "FY 2024-25", "FY2025-26", "2024-25": the financial year a CSR statement is about.
FINANCIAL_YEAR = re.compile(r"(?:FY\s?)?(20\d\d)\s?[-–]\s?(\d\d)\b")


# -- value functions: plain code, so the same facts always give the same value -----


def log_scale(n: float, full_at: float) -> float:
    """0 at 1, 1.0 at `full_at`, on a log scale: 10,000 staff with full_at 10,000 -> 1.0, 100 -> 0.5."""
    return 0.0 if n <= 1 else min(1.0, math.log10(n) / math.log10(full_at))


def growth_value(percent: float, span: float) -> float:
    """12-month headcount growth: -span% -> 0, flat -> 0.5, +span% -> 1. Layoffs lower it."""
    return max(0.0, min(1.0, 0.5 + percent / (2 * span)))


def funding_value(raised_on: date, today: date, months: list[int]) -> float:
    """Raised recently -> more likely to spend on visibility."""
    age = (today.year - raised_on.year) * 12 + today.month - raised_on.month
    return 1.0 if age <= months[0] else 0.5 if age <= months[1] else 0.1


# -- company data ---------------------------------------------------------------


def company_facts(company_ids: list[int], client: Any, cfg: dict[str, Any],
                  today: date | None = None) -> dict[int, list[Signal]]:
    """Signals from Crustdata's company record, for each company it knows."""
    today = today or date.today()
    out: dict[int, list[Signal]] = {}
    ids = sorted(set(company_ids))
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        body = {"filters": {"field": "crustdata_company_id", "type": "in", "value": chunk},
                "fields": COMPANY_FIELDS, "limit": len(chunk)}
        response = client.call("/company/search", body)
        for n, company in enumerate(response.data.get("companies") or []):
            out[company["crustdata_company_id"]] = _company_signals(company, f"companies[{n}]", response, cfg, today)
    return out


def _company_signals(c: dict[str, Any], path: str, response: ApiResponse, cfg: dict[str, Any],
                     today: date) -> list[Signal]:
    info = c.get("basic_info") or {}
    name, url = info.get("name") or "", info.get("professional_network_url")
    updated = (c.get("metadata") or {}).get("updated_at")

    def fact(field: str, value: Any, claim: str, record_date: str | None = updated) -> Evidence:
        return evidence.from_api("/company/search", response, f"{path}.{field}", value, claim, record_date, url)

    signals = []
    headcount = (c.get("headcount") or {}).get("total")
    if headcount:
        signals.append(Signal("company_size", round(log_scale(headcount, cfg["company_size_full_at"]), 4),
                              [fact("headcount.total", headcount, f"{name} has {headcount:,} employees")]))

    growth = ((c.get("headcount") or {}).get("growth_percent") or {}).get("12m")
    if growth is not None:
        signals.append(Signal("growth", round(growth_value(growth, cfg["growth_span_percent"]), 4),
                              [fact("headcount.growth_percent.12m", round(growth, 1),
                                    f"{name}'s headcount changed {growth:+.1f}% in 12 months")]))

    funding = c.get("funding") or {}
    raised, round_type = funding.get("last_fundraise_date"), funding.get("last_round_type") or ""
    # Secondary sales move existing shares: no new money for the company, so they don't count.
    if raised and "secondary" not in round_type:
        value = funding_value(date.fromisoformat(raised[:10]), today, cfg["funding_months"])
        signals.append(Signal("funding", value, [fact("funding.last_fundraise_date", raised,
                                                      f"{name} last raised money ({round_type or 'round'}) on {raised[:10]}",
                                                      raised[:10])]))

    symbols = ((c.get("revenue") or {}).get("public_markets") or {}).get("stock_symbols") or []
    markets = info.get("markets") or []
    if symbols:
        signals.append(Signal("listed", 1.0, [fact("revenue.public_markets.stock_symbols", symbols,
                                                   f"{name} is listed ({', '.join(symbols)})")]))
    elif markets == ["PRIVATE"]:
        signals.append(Signal("listed", 0.0, [fact("basic_info.markets", markets, f"{name} is privately held")]))

    followers = (c.get("followers") or {}).get("count")
    if followers:
        signals.append(Signal("social_presence", round(log_scale(followers, cfg["followers_full_at"]), 4),
                              [fact("followers.count", followers, f"{name} has {followers:,} LinkedIn followers")]))
    return signals


# -- alumni ---------------------------------------------------------------------


def alumni(candidates: list[Candidate], school: str, client: Any, cfg: dict[str, Any]) -> dict[int, list[Signal]]:
    """How many people who studied at `school` work at each company now (a count, dated today)."""
    out = {}
    for c in candidates:
        body = {"filters": {"op": "and", "conditions": [
                    {"field": "education.schools.school", "type": "(.)", "value": school},
                    {"field": "experience.employment_details.current.company_id", "type": "=", "value": c.company_id}]},
                "fields": ["crustdata_person_id"], "limit": 1}
        response = client.call("/person/search", body)
        count = response.data.get("total_count") or 0
        # A count describes the index when we asked, so the retrieval date is its date.
        fact = evidence.from_api("/person/search", response, "total_count", count,
                                 f"{count} people who studied at {school} work at {c.name} now",
                                 response.retrieved_at[:10])
        out[c.company_id] = [Signal("alumni", min(1.0, count / cfg["alumni_full_at"]), [fact])]
    return out


# -- CSR ------------------------------------------------------------------------


def csr(candidates: list[Candidate], client: Any) -> dict[int, list[Signal]]:
    """A CSR page on the company's own site that names a financial year. Unknown if there isn't one."""
    out = {}
    for c in candidates:
        if not c.domain:
            continue
        home_url = f"https://www.{c.domain}"
        home = _first(client.call("/web/enrich/live", {"urls": [home_url]}).data)
        link = csr_link(home.get("content") or "", home_url)
        if link is None:
            continue
        response = client.call("/web/enrich/live", {"urls": [link]})
        page = _first(response.data)
        quote = csr_passage(evidence.page_text(page.get("content") or ""))
        if quote is None:
            fact = evidence.cannot_verify(f"{c.name} publishes its CSR work", "the CSR page names no financial year",
                                          link, "web_fetch", response.retrieved_at)
            out[c.company_id] = [Signal("csr", None, [fact])]
            continue
        fact = evidence.from_web_fetch(page, response.retrieved_at, quote, f"{c.name} publishes its CSR work",
                                       quote, fy_start(quote))
        out[c.company_id] = [Signal("csr", 1.0 if fact.status == "verified" else None, [fact])]
    return out


def csr_link(html: str, base_url: str) -> str | None:
    """The first link on the page whose address or text says CSR."""
    for href, text in CSR_LINK.findall(html):
        if CSR_WORDS.search(href.replace("-", " ")) or CSR_WORDS.search(evidence.page_text(text)):
            if href.startswith("//"):
                return "https:" + href
            if href.startswith("/"):
                return base_url.rstrip("/") + href
            return href if href.startswith("http") else None
    return None


def csr_passage(text: str) -> str | None:
    """The sentence-sized passage that says CSR and names a financial year, as it appears on the page."""
    for match in CSR_WORDS.finditer(text):
        words = text[max(0, match.start() - 150): match.end() + 150].split()
        passage = " ".join(words[1:-1])   # drop the two edge words: the cut may split them
        if FINANCIAL_YEAR.search(passage) and CSR_WORDS.search(passage):
            return passage
    return None


def fy_start(passage: str) -> str:
    """'FY 2026-27' -> '2026-04-01': India's financial year starts on 1 April. The start is the
    earliest date the statement can be about, so the fact is never dated later than it could be."""
    return f"{FINANCIAL_YEAR.search(passage).group(1)}-04-01"


def _first(data: Any) -> dict[str, Any]:
    return (data or [{}])[0] if isinstance(data, list) else {}
