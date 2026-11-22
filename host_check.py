"""Host verification: is the organization behind this event real, before any outreach?

Why: without it the agent could be used to phish sponsors under a made-up event.

The check is one sourced fact: the host's own website, fetched live, names the host
(word for word). Verified only then; every other outcome is "cannot verify" with what
is missing, and outreach.start_campaign refuses to start.

If Crustdata knows the website's domain, its company ID rides along in the evidence
value. Crustdata is not required: small clubs and student societies often aren't in it.

Limits, stated plainly: this stops a missing, dead or unrelated website. It does not
prove the host controls the site it names; a person still approves every email.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import urlparse

import evidence
from evidence import cannot_verify
from records import Brief, Evidence


def domain_of(website: str) -> str | None:
    """'https://www.technova.org/about' -> 'technova.org'; None if it isn't a usable address."""
    url = website.strip()
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host) else None


def crustdata_company_id(identify_response: Any, domain: str) -> int | None:
    """The company ID Crustdata has for this exact domain, if any."""
    for item in identify_response or []:
        for match in item.get("matches") or []:
            data = match.get("company_data") or {}
            if ((data.get("basic_info") or {}).get("primary_domain") or "").lower() == domain:
                return data.get("crustdata_company_id")
    return None


def check_host(brief: Brief, client: Any, db: sqlite3.Connection | None = None,
               run_id: str | None = None) -> Evidence:
    """Return Evidence that the host is real (status 'verified') or why it can't be shown."""
    claim = f"{brief.host_name} is a real organization with a live website"
    if not brief.host_website:
        fact = cannot_verify(claim, "the brief has no website for the host")
        return _save(fact, db, run_id, brief)
    domain = domain_of(brief.host_website)
    if domain is None:
        fact = cannot_verify(claim, f"'{brief.host_website}' is not a usable website address")
        return _save(fact, db, run_id, brief)

    url = f"https://{domain}"
    response = client.call("/web/enrich/live", {"urls": [url]})
    result = next((r for r in response.data or [] if r.get("success") and r.get("content")), None)
    if result is None:
        fact = cannot_verify(claim, f"the website {url} could not be fetched", url, "web_fetch",
                             response.retrieved_at)
        return _save(fact, db, run_id, brief)

    company_id = crustdata_company_id(client.call("/company/identify", {"domains": [domain]}).data, domain)
    # The page's own fetch time is its record date: the site was live and said this then.
    fact = evidence.from_web_fetch(result, response.retrieved_at, brief.host_name, claim,
                                   {"domain": domain, "crustdata_company_id": company_id},
                                   evidence.unix_to_iso(result.get("timestamp")))
    if fact.status != "verified":
        fact.missing = f"the website {url} does not mention '{brief.host_name}'"
    return _save(fact, db, run_id, brief)


def _save(fact: Evidence, db: sqlite3.Connection | None, run_id: str | None, brief: Brief) -> Evidence:
    if db is not None and run_id is not None:
        evidence.save(db, run_id, f"host:{brief.host_name}", fact)
    return fact
