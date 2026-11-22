"""Find ~15 Instagram post/reel URLs via Crustdata web search, fetch each with
human_mode true, parse it, and write a summary table.

Usage:
    export CRUSTDATA_API_KEY=...
    python3 scripts/instagram_survey.py

Outputs reports/instagram_survey.csv and reports/instagram_survey.md.
All calls go through the cached client, so re-runs cost no credits.

Privacy: raw page HTML (which includes commenters' names and comment text)
is only held in the local, gitignored SQLite cache. Reports store
comment_count only, never commenter names or comment text, and the caption
as a length only.
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crustdata_client import CrustdataClient, CrustdataError  # noqa: E402
from instagram_parser import parse_post, parse_url  # noqa: E402

TARGET = 15
REPORTS = ROOT / "reports"

# Each search is sent with the site: operator in the query text, since the
# `site` body parameter did not keep results to instagram.com.
QUERIES = [
    "hackathon sponsors announced India",
    "hackathon India title sponsor announcement",
    "hackathon 2026 India sponsors reel",
    "national hackathon India registrations open",
    "hackathon powered by sponsor India",
    "hackathon winners announced India",
]
IG_POST = re.compile(r"^https?://(www\.)?instagram\.com/(?:[^/]+/)?(p|reel|reels|tv)/[^/?#]+")

COLUMNS = [
    "url", "kind", "parse_status", "handle", "posted_at", "caption_length",
    "comment_count", "time_tag", "time_delta_seconds", "time_disagrees",
    "datePublished", "uploadDate", "fetch_success", "http_status", "page_title",
    "content_chars",
]


def canonical(url: str) -> str:
    m = IG_POST.match(url)
    return m.group(0).replace("http://", "https://").rstrip("/") + "/"


def collect_urls(client: CrustdataClient) -> tuple[list[str], list[dict]]:
    urls: list[str] = []
    log = []
    for q in QUERIES:
        if len(urls) >= TARGET:
            break
        query = f"{q} site:instagram.com"
        try:
            resp = client.web_search({"query": query})
        except CrustdataError as err:
            log.append({"query": query, "error": f"HTTP {err.status}"})
            continue
        results = resp.get("results") or []
        found = [canonical(r["url"]) for r in results if IG_POST.match(r.get("url") or "")]
        new = [u for u in dict.fromkeys(found) if u not in urls]
        urls.extend(new)
        log.append({"query": query, "results": len(results), "instagram_post_urls": len(found), "new": len(new)})
    return urls[:TARGET], log


def survey_row(client: CrustdataClient, url: str) -> dict:
    kind, _ = parse_url(url)
    row = {c: None for c in COLUMNS} | {"url": url, "kind": kind}
    try:
        resp = client.web_fetch({"urls": [url], "human_mode": True})
    except CrustdataError as err:
        return row | {"parse_status": "no_post_object", "fetch_success": False, "http_status": err.status}
    r = resp[0] if isinstance(resp, list) and resp else {}
    content = r.get("content") or ""
    row |= {
        "fetch_success": r.get("success"),
        "http_status": r.get("http_status"),
        "page_title": r.get("title"),
        "content_chars": len(content),
    }
    if not (r.get("success") and content):
        return row | {"parse_status": "no_post_object"}
    p = parse_post(content, url)
    taken_at = p["taken_at"]
    return row | {
        "parse_status": p["parse_status"],
        "handle": p["handle"],
        "posted_at": datetime.fromtimestamp(taken_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(taken_at, int) else None,
        "caption_length": len(p["caption"]) if p["caption"] is not None else None,
        "comment_count": p["comment_count"],
        "time_tag": p["time_tag"],
        "time_delta_seconds": p["time_delta_seconds"],
        "time_disagrees": p["time_disagrees"],
        "datePublished": p["datePublished"],
        "uploadDate": p["uploadDate"],
    }


def _cell(v) -> str:
    return "" if v is None else str(v).replace("|", "\\|")


def write_reports(rows: list[dict], search_log: list[dict]) -> str:
    REPORTS.mkdir(exist_ok=True)
    with open(REPORTS / "instagram_survey.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    counts = {s: sum(r["parse_status"] == s for r in rows) for s in ("ok", "gated", "shortcode_mismatch", "no_post_object")}
    pct = lambda k: f"{counts[k]}/{n} ({100 * counts[k] / n:.0f}%)" if n else "0/0"
    disagree = sum(bool(r["time_disagrees"]) for r in rows)
    checked = sum(r["time_disagrees"] is not None for r in rows)

    lines = [
        "# Instagram fetch survey",
        "",
        f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}. "
        "Each URL fetched via /web/enrich/live with human_mode true.",
        "",
        f"- Success rate (parse_status ok): {pct('ok')}",
        f"- Gated rate: {pct('gated')}",
        f"- shortcode_mismatch: {pct('shortcode_mismatch')}; no_post_object: {pct('no_post_object')}",
        f"- taken_at vs <time> over 60 s apart: {disagree} of {checked} checked",
        "",
        "| URL | kind | parse_status | handle | posted_at | caption length | comments | time Δ s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(_cell(r[c]) for c in (
            "url", "kind", "parse_status", "handle", "posted_at", "caption_length",
            "comment_count", "time_delta_seconds")) + " |")
    lines += ["", "## Searches", "", "| query | results | instagram post URLs | new |", "|---|---|---|---|"]
    for s in search_log:
        lines.append(f"| {_cell(s['query'])} | {_cell(s.get('results', s.get('error')))} | "
                     f"{_cell(s.get('instagram_post_urls'))} | {_cell(s.get('new'))} |")
    md = "\n".join(lines) + "\n"
    (REPORTS / "instagram_survey.md").write_text(md, encoding="utf-8")
    return md


def main() -> int:
    client = CrustdataClient()
    try:
        urls, search_log = collect_urls(client)
        for s in search_log:
            print("search:", s)
        print(f"{len(urls)} Instagram post/reel URLs")
        rows = []
        for i, url in enumerate(urls, 1):
            row = survey_row(client, url)
            print(f"[{i}/{len(urls)}] {row['parse_status']:<18} {url}")
            rows.append(row)
    finally:
        client.close()
    print(write_reports(rows, search_log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
