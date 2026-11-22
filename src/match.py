"""Block 3b, step 2: resolve sponsor names to Crustdata company IDs.

Each name is looked up as written and in its normalised form with
/company/identify ({"names": [...]}, up to 25 per call). Response fields used,
as the fixture shows them: matched_on, matches[].confidence_score,
matches[].company_data.crustdata_company_id, matches[].company_data.basic_info.name.

A company is taken only when one clearly wins: the top confidence_score is at
least `min_confidence` and no other company is within `ambiguity_margin` of it.
The one exception: records with the same name where only one has a website.
Anything else is ambiguous (no ID) or unmatched. We never pick one by guessing.
"""

from __future__ import annotations

import re
from typing import Any

# Longest first, so "private limited" is stripped before "limited"-like parts.
LEGAL_SUFFIXES = ["private limited", "technologies", "software", "pvt", "ltd", "inc", "llp"]


def normalise(name: str) -> tuple[str, list[str]]:
    """Lowercase and strip legal suffixes from the end.

    Returns (normalised name, aliases). Aliases keep the original and every
    intermediate form, so nothing written in the caption is lost.
    """
    aliases = [name]
    form = re.sub(r"\s+", " ", re.sub(r"[.,]", " ", name.lower())).strip()
    aliases.append(form)
    stripped = True
    while stripped:
        stripped = False
        for suffix in LEGAL_SUFFIXES:
            if form.endswith(" " + suffix):
                form = form[: -len(suffix)].strip()
                aliases.append(form)
                stripped = True
                break
    return form, list(dict.fromkeys(aliases))


def _queries(name: str, normalised_name: str) -> list[str]:
    return list(dict.fromkeys([name, normalised_name]))


def identify_all(queries: list[str], client: Any, batch_size: int = 25) -> dict[str, list[dict]]:
    """Look up every query string; returns {query: matches}. Queries without an entry get []."""
    found: dict[str, list[dict]] = {q: [] for q in queries}
    unique = list(dict.fromkeys(queries))
    for i in range(0, len(unique), batch_size):
        response = client.request("/company/identify", {"names": unique[i:i + batch_size]})
        for entry in response or []:
            if entry.get("matched_on") in found:
                found[entry["matched_on"]] = entry.get("matches") or []
    return found


def decide(matches: list[dict], min_confidence: float, ambiguity_margin: float) -> dict[str, Any]:
    """Pick a company only if one clearly wins. Same company seen twice keeps its best score."""
    best: dict[int, tuple[float, str | None, str | None]] = {}
    for m in matches:
        data = m.get("company_data") or {}
        company_id = data.get("crustdata_company_id")
        score = m.get("confidence_score")
        if company_id is None or not isinstance(score, (int, float)):
            continue
        info = data.get("basic_info") or {}
        if company_id not in best or score > best[company_id][0]:
            best[company_id] = (float(score), info.get("name"), info.get("primary_domain"))

    if not best:
        return {"match_status": "unmatched", "crustdata_company_id": None,
                "company_name": None, "confidence_score": None, "candidates": 0}

    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    top_score = ranked[0][1][0]
    close = [cid for cid, (s, _, _) in ranked if s >= top_score - ambiguity_margin]
    # Two records with the same name where only one has a website: the other is usually a
    # duplicate stub (seen live: "ITC Limited" itcportal.com, and a second "ITC Limited" with none).
    if len(close) > 1 and len({normalise(best[cid][1] or "")[0] for cid in close}) == 1:
        with_site = [cid for cid in close if best[cid][2]]
        if len(with_site) == 1:
            close = with_site
    top_id = close[0]
    top_score, top_name, _ = best[top_id]
    if top_score < min_confidence or len(close) > 1:
        return {"match_status": "ambiguous", "crustdata_company_id": None,
                "company_name": None, "confidence_score": top_score, "candidates": len(ranked)}
    return {"match_status": "matched", "crustdata_company_id": top_id,
            "company_name": top_name, "confidence_score": top_score, "candidates": len(ranked)}


def match_names(names: list[str], client: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve each sponsor name as written. One result per input name, in order."""
    cfg = config["match"]
    prepared = []
    for name in names:
        normalised_name, aliases = normalise(name)
        prepared.append((name, normalised_name, aliases))

    all_queries = [q for name, norm, _ in prepared for q in _queries(name, norm)]
    found = identify_all(all_queries, client, cfg.get("identify_batch_size", 25))

    results = []
    for name, norm, aliases in prepared:
        matches = [m for q in _queries(name, norm) for m in found[q]]
        decision = decide(matches, cfg["min_confidence"], cfg["ambiguity_margin"])
        results.append({"sponsor_name_as_written": name, "normalised_name": norm,
                        "aliases": aliases, **decision})
    return results


def by_company(results: list[dict[str, Any]]) -> dict[int, list[str]]:
    """Group matched names by company ID: two spellings of one company become one entry."""
    groups: dict[int, list[str]] = {}
    for r in results:
        if r["match_status"] == "matched":
            groups.setdefault(r["crustdata_company_id"], []).append(r["sponsor_name_as_written"])
    return groups
