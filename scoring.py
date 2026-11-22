"""Block 4: score and rank candidate sponsors for a Brief. Pure code: no model, no API.

Hard filters run first. An excluded candidate is kept with its reason and is not scored.

A signal counts as known only when it has a value AND at least one verified Evidence
record (handbook: every fact is sourced). Unknown signals are left out and the known
weights rescaled, so missing data never reads as a low score; `confidence` is the
share of the profile's weight that was known. Confidence is shown next to the score
and only breaks ties: it never pushes a candidate down the ranking.

"Category conflicts" here means categories the host rules out in the brief (e.g. a
competitor of an exclusive sponsor) plus `restricted_categories` in config.yaml.
They are matched against Candidate.industries.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from records import Brief, Candidate
from src.match import normalise

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
_FILLER_WORDS = {"brands", "brand", "companies", "company", "products", "sponsors", "industry"}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["scoring"]


def category_key(category: str) -> str:
    """'Alcohol brands' and 'alcohol' -> 'alcohol'; 'Banks' -> 'bank'. Same words, same key."""
    words = [w for w in category.lower().split() if w not in _FILLER_WORDS]
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]
    return " ".join(words)


def _excluded_category(candidate: Candidate, brief: Brief, config: dict[str, Any]) -> str | None:
    keywords = {category_key(k): v for k, v in (config.get("category_keywords") or {}).items()}
    rules = [(c, "restricted for every event") for c in config.get("restricted_categories") or []]
    rules += [(c, "excluded by the host") for c in brief.excluded_categories]
    for category, why in rules:
        key = category_key(category)
        if not key:  # e.g. just "brands": nothing to match, and "" would match everything
            continue
        for industry in candidate.industries:
            if any(word in industry.lower() for word in keywords.get(key, [key])):
                return f"industry '{industry}' is in category '{category}', {why}"
    return None


def hard_filter(candidate: Candidate, brief: Brief, config: dict[str, Any]) -> str | None:
    """The reason this candidate must not be pitched, or None."""
    current = {normalise(s)[0] for s in brief.current_sponsors}
    if normalise(candidate.name)[0] in current:
        return "already a sponsor of this event"
    return _excluded_category(candidate, brief, config)


def score(candidate: Candidate, brief: Brief, config: dict[str, Any]) -> Candidate:
    """Return a copy of the candidate with excluded_reason, or with score and confidence set."""
    if brief.profile not in config["profiles"]:
        raise ValueError(f"no weights for profile {brief.profile!r}")
    weights: dict[str, float] = config["profiles"][brief.profile]

    by_name = {}
    for s in candidate.signals:
        if s.name not in weights:
            raise ValueError(f"unknown signal {s.name!r} (not a weight in config.yaml)")
        if s.name in by_name:
            raise ValueError(f"signal {s.name!r} given twice for company {candidate.company_id}")
        if s.value is not None and not 0 <= s.value <= 1:
            raise ValueError(f"signal {s.name!r} = {s.value} is outside 0..1")
        by_name[s.name] = s

    reason = hard_filter(candidate, brief, config)
    if reason:
        return replace(candidate, excluded_reason=reason, score=None, confidence=None)

    known_weight = weighted_sum = 0.0
    for name, weight in weights.items():  # config order, so the sum is always added the same way
        s = by_name.get(name)
        if s and s.value is not None and any(e.status == "verified" for e in s.evidence):
            known_weight += weight
            weighted_sum += weight * s.value
    confidence = round(known_weight / sum(weights.values()), 4)
    value = round(weighted_sum / known_weight, 4) if known_weight else None
    return replace(candidate, excluded_reason=None, score=value, confidence=confidence)


def rank(candidates: list[Candidate], brief: Brief, config: dict[str, Any]) -> list[Candidate]:
    """Score every candidate and sort best first: scored, then unscored, then excluded.

    Ties break on confidence, then company ID, so the same inputs always give the same order.
    """
    seen: set[int] = set()
    scored = []
    for c in candidates:
        if c.company_id in seen:
            scored.append(replace(c, excluded_reason=f"duplicate of company {c.company_id}",
                                  score=None, confidence=None))
            continue
        seen.add(c.company_id)
        scored.append(score(c, brief, config))
    return sorted(scored, key=lambda c: (c.excluded_reason is not None, c.score is None,
                                         -(c.score or 0), -(c.confidence or 0), c.company_id))
