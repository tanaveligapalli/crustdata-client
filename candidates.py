"""Block 3: candidate pools. A Brief in, one deduplicated Candidate per company out.

Pools (each says how a company was found, in Candidate.found_by):
  hiring             companies posting sponsorship / partnerships / brand / event-marketing
                     jobs in the country in the last N days (fresh "has a budget now" signal)
  past_sponsor       companies named as sponsors in LinkedIn posts about similar events
                     (post index, ~2 months behind) and in Instagram posts found by web search
  host_past_sponsor  the host's own past sponsors, from the brief
  lookalike          companies in the same industries as the host's past sponsors
  company_search     companies in the profile's industries (config.yaml)

The Crustdata company ID is the only key: two names for one company become one candidate.
Pools only produce facts (Evidence). Signal values are plain counts of verified facts, so
the same facts always give the same values. A company with no verified fact for a signal
gets no value for it: unknown, never zero.

A source that keeps failing (SourceUnavailable) doesn't stop the other pools; its name is
returned in `failed_pools` so the run can be held for a person instead of reading the
silence as "no candidates".
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import evidence
import instagram_sponsors as ig
from crustdata_client import SourceUnavailable
from instagram_parser import parse_post
from records import Brief, Candidate, Evidence, Signal
from scoring import company_key
from src import extract, match

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
IG_POST_URL = re.compile(r"^https://(www\.)?instagram\.com/(p|reel)/[\w-]+/?")

CallModel = Callable[[str, str], tuple[str, float | None]]


@dataclass
class Hit:
    """One pool found one company. `facts` say why; `signal` names what they support."""

    company_id: int
    name: str
    pool: str
    signal: str | None = None
    facts: list[Evidence] = field(default_factory=list)
    domain: str | None = None
    industries: list[str] | None = None   # None = not known yet: describe() looks it up


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["candidates"]


def _since(days: int, today: date | None = None) -> str:
    return ((today or date.today()) - timedelta(days=days)).isoformat()


def _any_of(field_name: str, op: str, values: list[str]) -> dict[str, Any]:
    return {"op": "or", "conditions": [{"field": field_name, "type": op, "value": v} for v in values]}


# -- pool: hiring -------------------------------------------------------------


def hiring_pool(brief: Brief, client: Any, cfg: dict[str, Any], today: date | None = None) -> list[Hit]:
    """Companies with matching job postings. Each posting is a dated fact (its LinkedIn URL).

    A posting in the event's city is also a city_presence fact.
    """
    hiring = cfg["hiring"]
    body = {
        "filters": {"op": "and", "conditions": [
            {"field": "location.country", "type": "=", "value": cfg["country"]},
            {"field": "metadata.date_added", "type": "=>", "value": _since(hiring["days"], today)},
            # LinkedIn postings carry a company ID; many other job boards don't.
            {"field": "job_details.source", "type": "=", "value": "professional_network"},
            _any_of("job_details.title", "[.]", hiring["title_keywords"]),
        ]},
        "fields": ["job_details.title", "job_details.url", "metadata.date_added", "location.city",
                   "company.basic_info.crustdata_company_id", "company.basic_info.name",
                   "company.basic_info.primary_domain", "company.basic_info.industries"],
        "sorts": [{"field": "metadata.date_added", "order": "desc"}],
        "limit": hiring["limit"],
    }
    response = client.call("/job/search", body)

    hits = []
    for i, job in enumerate(response.data.get("job_listings") or []):
        company = job["company"]["basic_info"]
        if company.get("crustdata_company_id") is None:
            continue
        company_id, name = company["crustdata_company_id"], company.get("name") or ""
        about = {"domain": company.get("primary_domain"), "industries": company.get("industries") or []}
        title, url = job["job_details"]["title"], job["job_details"]["url"]
        posted = job["metadata"]["date_added"]

        fact = evidence.from_api("/job/search", response, f"job_listings[{i}].job_details.title", title,
                                 f"{name} posted a '{title}' job", posted, source_url=url)
        hits.append(Hit(company_id, name, "hiring", "active_hiring", [fact], **about))

        city = (job.get("location") or {}).get("city")
        if brief.city and city and city.casefold() == brief.city.casefold():
            fact = evidence.from_api("/job/search", response, f"job_listings[{i}].location.city", city,
                                     f"{name} is hiring in {city}", posted, source_url=url)
            hits.append(Hit(company_id, name, "hiring", "city_presence", [fact], **about))
    return hits


# -- pool: past sponsors ------------------------------------------------------


def sponsors_in_post(post: dict[str, Any], retrieved_at: str, source_type: str, db: sqlite3.Connection,
                     run_id: str, client: Any, ig_config: dict[str, Any], call_model: CallModel) -> list[Hit]:
    """Sponsors named in one post's text, via block 3b: the model extracts, code checks every
    name is in the text, and names resolve to company IDs only on a clear match.

    `post` has the instagram_parser.parse_post keys: url, parse_status, shortcode, handle,
    caption, taken_at. Unresolved brands are saved as "cannot verify" under subject "unresolved".
    """
    extracted = extract.extract(post, db, run_id, call_model)
    names = [m["name"] for m in extracted["sponsor_mentions"]]
    matches = match.match_names(names, client, ig_config) if names else []
    facts, leads = ig.build_records(post, extracted, matches, retrieved_at)

    for lead in leads:
        if lead.kind == "sponsor":
            evidence.save(db, run_id, "unresolved", evidence.cannot_verify(
                f"{lead.sponsor_name_as_written} sponsored {lead.event_name or 'an event'}", lead.reason,
                lead.source_url, source_type, retrieved_at, lead.evidence_text or "", lead.sponsor_name_as_written))

    return [Hit(f.value["crustdata_company_id"], f.value["sponsor_name_as_written"], "past_sponsor",
                "past_sponsorships", [replace(f, source_type=source_type)])
            for f in facts]


def linkedin_sponsor_pool(brief: Brief, client: Any, db: sqlite3.Connection, run_id: str,
                          cfg: dict[str, Any], ig_config: dict[str, Any], call_model: CallModel,
                          today: date | None = None) -> list[Hit]:
    """Company posts about sponsoring this kind of event in the country (indexed LinkedIn posts)."""
    past = cfg["past_sponsors"]
    body = {
        "filters": {"op": "and", "conditions": [
            {"field": "actor.actor_type", "type": "=", "value": "company"},
            {"field": "date_posted", "type": "=>", "value": _since(past["days"], today)},
            _any_of("text", "[.]", past["sponsor_words"]),
            _any_of("text", "[.]", past["event_words"][brief.profile]),
            # Posts by Indian companies, or posts that name the country.
            {"op": "or", "conditions": [
                {"field": "actor.location.country", "type": "=", "value": cfg["country_code"]},
                {"field": "text", "type": "[.]", "value": cfg["country"]},
            ]},
        ]},
        "fields": ["share_url", "text", "date_posted", "actor"],
        "sorts": [{"field": "date_posted", "order": "desc"}],
        "limit": past["linkedin_posts"],
    }
    response = client.call("/social_post/search", body)

    hits = []
    for p in response.data.get("posts") or []:
        post = {
            "url": p["share_url"], "parse_status": "ok", "shortcode": p["share_url"],
            "handle": (p.get("actor") or {}).get("name"), "caption": p.get("text") or "",
            "taken_at": _unix(p.get("date_posted")),
        }
        hits += sponsors_in_post(post, response.retrieved_at, "linkedin_post", db, run_id, client,
                                 ig_config, call_model)
    return hits


def instagram_sponsor_pool(brief: Brief, client: Any, db: sqlite3.Connection, run_id: str,
                           cfg: dict[str, Any], ig_config: dict[str, Any], call_model: CallModel) -> list[Hit]:
    """Instagram posts found by web search (snippets are only leads), fetched and read in full."""
    past = cfg["past_sponsors"]
    urls: list[str] = []
    for word in past["event_words"][brief.profile]:
        query = f"{word} sponsors {brief.city or cfg['country']}"
        results = client.call("/web/search/live", {"query": query, "sources": ["web"],
                                                   "site": "instagram.com/p"}).data.get("results") or []
        for r in results:
            m = IG_POST_URL.match(r.get("url") or "")
            if m and m.group(0) not in urls:
                urls.append(m.group(0))

    hits = []
    for url in urls[: past["instagram_posts"]]:
        response = client.call("/web/enrich/live", {"urls": [url]})
        page = (response.data or [{}])[0]
        if not (page.get("success") and page.get("content")):
            continue
        post = {"url": url, **parse_post(page["content"], url)}
        hits += sponsors_in_post(post, response.retrieved_at, "instagram_post", db, run_id, client,
                                 ig_config, call_model)
    return hits


# -- pools: company search and look-alikes ------------------------------------


def company_search_pool(brief: Brief, client: Any, cfg: dict[str, Any], industries: list[str],
                        pool: str) -> list[Hit]:
    """Companies in these industries with offices in the event's city (or the country)."""
    search = cfg["company_search"]
    conditions = [
        {"field": "locations.country", "type": "=", "value": cfg["country"]},
        {"field": "basic_info.industries", "type": "in", "value": industries},
        {"field": "basic_info.employee_count_range", "type": "in", "value": search["employee_ranges"]},
    ]
    if brief.city:
        conditions.append({"field": "locations.all_office_addresses", "type": "(.)", "value": brief.city})
    body = {"filters": {"op": "and", "conditions": conditions},
            "fields": DESCRIBE_FIELDS,
            "sorts": [{"field": "headcount.total", "order": "desc"}],
            "limit": search["limit"]}
    companies = client.call("/company/search", body).data.get("companies") or []
    return [Hit(c["crustdata_company_id"], c["basic_info"]["name"], pool, **_about(c)) for c in companies]


def describe(hits: list[Hit], client: Any) -> list[Hit]:
    """Fill in domain and industries (scoring's category filters read them) where a pool
    didn't return them. Costs ~0.13 credits per company, so only unknown ones are looked up."""
    ids = sorted({h.company_id for h in hits if h.industries is None})
    about: dict[int, dict[str, Any]] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        body = {"filters": {"field": "crustdata_company_id", "type": "in", "value": chunk},
                "fields": DESCRIBE_FIELDS, "limit": len(chunk)}
        for c in client.call("/company/search", body).data.get("companies") or []:
            about[c["crustdata_company_id"]] = {"name": c["basic_info"]["name"], **_about(c)}
    return [replace(h, **about[h.company_id]) if h.company_id in about else h for h in hits]


DESCRIBE_FIELDS = ["crustdata_company_id", "basic_info.name", "basic_info.primary_domain",
                   "taxonomy.professional_network_industries"]


def _about(company: dict[str, Any]) -> dict[str, Any]:
    return {"domain": company["basic_info"].get("primary_domain"),
            "industries": (company.get("taxonomy") or {}).get("professional_network_industries") or []}


def host_sponsor_pools(brief: Brief, client: Any, cfg: dict[str, Any],
                       ig_config: dict[str, Any]) -> list[Hit]:
    """The host's past sponsors themselves, plus companies in the same industries."""
    if not brief.past_sponsors:
        return []
    resolved = match.by_company(match.match_names(brief.past_sponsors, client, ig_config))
    hits = [Hit(company_id, names[0], "host_past_sponsor") for company_id, names in resolved.items()]

    hits = describe(hits, client)
    industries = sorted({i for h in hits for i in h.industries or []})
    if industries:
        hits += company_search_pool(brief, client, cfg, industries, "lookalike")
    return hits


# -- merging ------------------------------------------------------------------


def merge(hits: list[Hit], brief: Brief, cfg: dict[str, Any]) -> list[Candidate]:
    """One Candidate per company ID, with signals counted from verified facts."""
    full_at = cfg["signals_full_at"]  # verified facts needed for a signal value of 1.0
    host = company_key(brief.host_name)
    by_id: dict[int, Candidate] = {}
    facts: dict[tuple[int, str], list[Evidence]] = {}

    for hit in hits:
        if company_key(hit.name) == host:
            continue  # never pitch the host to itself
        c = by_id.setdefault(hit.company_id, Candidate(
            hit.company_id, hit.name, hit.domain, industries=hit.industries or []))
        if hit.pool not in c.found_by:
            c.found_by.append(hit.pool)
        if hit.signal:
            facts.setdefault((hit.company_id, hit.signal), []).extend(hit.facts)

    for (company_id, signal), signal_facts in facts.items():
        verified = {f.source_url for f in signal_facts if f.status == "verified"}
        value = min(1.0, len(verified) / full_at[signal]) if verified else None
        by_id[company_id].signals.append(Signal(signal, value, signal_facts))

    return [by_id[k] for k in sorted(by_id)]


# -- the whole block ----------------------------------------------------------


def find_candidates(brief: Brief, client: Any, db: sqlite3.Connection, run_id: str,
                    cfg: dict[str, Any], ig_config: dict[str, Any],
                    call_model: CallModel | None) -> tuple[list[Candidate], list[str]]:
    """Run every pool, merge by company ID and save every fact to the evidence ledger.

    Returns (candidates, failed_pools). call_model=None skips the two post pools
    (they need the model to read sponsor names); they are then listed as failed.
    """
    pools: list[tuple[str, Callable[[], list[Hit]]]] = [
        ("hiring", lambda: hiring_pool(brief, client, cfg)),
        ("host_past_sponsors", lambda: host_sponsor_pools(brief, client, cfg, ig_config)),
        ("company_search", lambda: company_search_pool(
            brief, client, cfg, cfg["company_search"]["industries"][brief.profile], "company_search")),
    ]
    if call_model is not None:
        pools += [
            ("linkedin_posts", lambda: linkedin_sponsor_pool(brief, client, db, run_id, cfg, ig_config, call_model)),
            ("instagram_posts", lambda: instagram_sponsor_pool(brief, client, db, run_id, cfg, ig_config, call_model)),
        ]

    hits: list[Hit] = []
    failed = [] if call_model else ["linkedin_posts (no model)", "instagram_posts (no model)"]
    for name, pool in pools:
        try:
            hits += pool()
        except SourceUnavailable:
            failed.append(name)

    candidates = merge(describe(hits, client), brief, cfg)
    for c in candidates:
        evidence.save(db, run_id, f"company:{c.company_id}", [f for s in c.signals for f in s.evidence])
    return candidates, failed


def _unix(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso).timestamp())
