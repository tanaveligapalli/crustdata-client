"""Call each Crustdata endpoint we use and save its raw response to tests/fixtures/.

Usage:
    export CRUSTDATA_API_KEY=...
    python3 scripts/capture_fixtures.py

Only missing fixtures are captured; delete a file to capture it again. Tests
depend on the saved files, so existing ones are never overwritten.

Request bodies use only parameters documented at https://docs.crustdata.com
(API version 2025-11-01). Responses are written exactly as returned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crustdata_client import CrustdataClient, CrustdataError  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

CALLS = [
    # (fixture name, client method, request body)
    ("company_identify", "company_identify", {"domains": ["serverobotics.com"]}),
    # Block 3b looks sponsors up by name: capture what a names lookup returns.
    (
        "company_identify_names",
        "company_identify",
        {"names": ["ITC Limited", "Trainocate Networks India Pvt Ltd", "WOW Momo"]},
    ),
    (
        "company_search",
        "company_search",
        {
            "filters": {"field": "basic_info.primary_domain", "type": "=", "value": "hubspot.com"},
            "fields": ["basic_info", "headcount", "funding"],
            "limit": 1,
        },
    ),
    (
        # A decision-maker at a sponsor: partnerships people at HubSpot.
        "person_search",
        "person_search",
        {
            "filters": {"op": "and", "conditions": [
                {"field": "experience.employment_details.current.company_website_domain", "type": "=", "value": "hubspot.com"},
                {"field": "experience.employment_details.current.title", "type": "(.)", "value": "partnerships"},
            ]},
            "fields": ["crustdata_person_id", "basic_profile", "social_handles", "contact"],
            "limit": 1,
        },
    ),
    (
        "job_search",
        "job_search",
        {
            "filters": {"field": "job_details.category", "type": "=", "value": "Engineering"},
            "fields": ["job_details.title", "job_details.url", "metadata.date_added", "location.country"],
            "sorts": [{"field": "metadata.date_added", "order": "desc"}],
            "limit": 5,
        },
    ),
    (
        # Past sponsorship evidence: the newest company post about sponsoring a conference.
        "social_post_search",
        "social_post_search",
        {
            "filters": {"op": "and", "conditions": [
                {"field": "text", "type": "[.]", "value": "sponsor"},
                {"field": "text", "type": "[.]", "value": "conference"},
                {"field": "actor.actor_type", "type": "=", "value": "company"},
            ]},
            "fields": ["share_url", "text", "date_posted", "actor", "engagement", "mentions", "metadata"],
            "sorts": [{"field": "date_posted", "order": "desc"}],
            "limit": 1,
        },
    ),
    (
        "web_search",
        "web_search",
        {"query": "hackathon sponsors announced India", "site": "instagram.com"},
    ),
    (
        "web_fetch",
        "web_fetch",
        {"urls": ["https://www.instagram.com/p/DTNUCNQCL_w/"]},
    ),
]


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    client = CrustdataClient()
    failures = 0
    try:
        for name, method, body in CALLS:
            if (FIXTURES / f"{name}.json").exists():
                print(f"skip {name} (already saved)")
                continue
            try:
                response = getattr(client, method)(body)
            except CrustdataError as err:
                failures += 1
                print(f"FAIL {name}: HTTP {err.status} {err.body!r}", file=sys.stderr)
                continue
            out = FIXTURES / f"{name}.json"
            out.write_text(json.dumps(response, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"ok   {name} -> {out.relative_to(ROOT)}")
    finally:
        client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
