"""Run block 3b over every Instagram post in the Crustdata cache and print what it found.

Usage:
    set CRUSTDATA_API_KEY and ANTHROPIC_API_KEY, pip install anthropic pyyaml
    python scripts/instagram_sponsor_summary.py [--credit-limit 20]

Posts come from the local response cache (no new fetches). Sponsor names are
looked up live with /company/identify (cached afterwards), and captions go to
the model once each (cached in agent.sqlite3). Every call is in the cost ledger.
Thresholds come from config.yaml; this script never changes them.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import instagram_sponsors as ig  # noqa: E402
from crustdata_client import DEFAULT_CACHE_PATH, CrustdataClient  # noqa: E402
from instagram_parser import parse_post  # noqa: E402
from src import extract  # noqa: E402


def cached_posts(cache_path: Path = DEFAULT_CACHE_PATH) -> list[tuple[dict, str]]:
    """Every cached web fetch of an Instagram post: (parsed post with url, fetched_at)."""
    if not cache_path.exists():
        return []
    db = sqlite3.connect(str(cache_path))
    rows = db.execute(
        "SELECT request_body, response, created_at FROM responses WHERE path = '/web/enrich/live' ORDER BY created_at"
    ).fetchall()
    db.close()
    posts, seen = [], set()
    for body, response, created_at in rows:
        for r in json.loads(response) or []:
            url = r.get("url")
            if not url or "instagram.com" not in url or not r.get("success") or not r.get("content"):
                continue
            p = {"url": url, **parse_post(r["content"], url)}
            if p.get("shortcode") in seen:
                continue
            seen.add(p.get("shortcode"))
            posts.append((p, datetime.fromtimestamp(created_at, timezone.utc).isoformat(timespec="seconds")))
    return posts


def missing_prerequisites() -> list[str]:
    missing = []
    if not os.environ.get("CRUSTDATA_API_KEY"):
        missing.append("CRUSTDATA_API_KEY is not set (needed for /company/identify)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY is not set (needed for caption extraction)")
    try:
        import anthropic  # noqa: F401
    except ImportError:
        missing.append("the anthropic package is not installed (pip install anthropic)")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credit-limit", type=float, default=20.0)
    parser.add_argument("--run-id", default=f"3b-summary-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
    args = parser.parse_args()

    posts = cached_posts()
    ok_posts = [(p, f) for p, f in posts if p["parse_status"] == "ok"]
    print(f"{len(posts)} cached Instagram posts, {len(ok_posts)} with parse_status ok")
    missing = missing_prerequisites()
    if missing:
        print("Not run:\n  " + "\n  ".join(missing))
        return 2

    config = ig.load_config()
    db = agent_db.connect()
    client = CrustdataClient(run_id=args.run_id, ledger_db=db, credit_limit=args.credit_limit)
    call_model = extract.anthropic_caller(config)

    rows = []
    counts = {"posts_processed": 0, "logo_only": 0, "extraction_failed": 0, "mentions_extracted": 0,
              "matched": 0, "ambiguous": 0, "unmatched": 0, "mentions_dropped_by_validation": 0,
              "other_fields_dropped": 0, "evidence_records": 0, "leads": 0, "sponsor_asks": 0}
    try:
        for p, fetched_at in ok_posts:
            result = ig.process_post(p, fetched_at, db, args.run_id, client, config, call_model)
            ex = result["extracted"]
            counts["posts_processed"] += 1
            counts["logo_only"] += ex["extraction_status"] == "logo_only"
            counts["extraction_failed"] += ex["extraction_status"] == "failed"
            counts["mentions_extracted"] += len(ex["sponsor_mentions"])
            counts["mentions_dropped_by_validation"] += sum(d["field"] == "sponsor_mentions" for d in ex["dropped"])
            counts["other_fields_dropped"] += sum(d["field"] != "sponsor_mentions" for d in ex["dropped"])
            counts["evidence_records"] += len(result["evidence"])
            counts["leads"] += len(result["leads"])
            counts["sponsor_asks"] += ex["sponsor_ask"]
            written = {e.value["sponsor_name_as_written"].lower() + ":" + e.value["role"] for e in result["evidence"]}
            for mention, m in zip(ex["sponsor_mentions"], result["matches"]):
                counts[m["match_status"]] += 1
                rows.append([p["url"], ex["event_name"], mention["name"], mention["role"], m["match_status"],
                             m["crustdata_company_id"],
                             f"{mention['name'].lower()}:{mention['role']}" in written])
            if not ex["sponsor_mentions"]:
                rows.append([p["url"], ex["event_name"], "", "", ex["extraction_status"], None, False])
    finally:
        client.close()

    header = ["url", "event_name", "sponsor", "role", "match_status", "company_id", "ledger_written"]
    print("\n| " + " | ".join(header) + " |\n|" + "---|" * len(header))
    for r in rows:
        print("| " + " | ".join("" if v is None else str(v).replace("|", "\\|") for v in r) + " |")

    spent = db.execute(
        "SELECT provider, COALESCE(SUM(credits), 0), COALESCE(SUM(cost_usd), 0), COUNT(*) FROM ledger WHERE run_id = ? GROUP BY provider",
        (args.run_id,),
    ).fetchall()
    print("\nCounts:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("Spend this run:")
    for provider, credits, usd, calls in spent:
        print(f"  {provider}: {calls} calls, {credits:g} credits, ${usd:.4f}")
    print(f"Thresholds used: {config['match']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
