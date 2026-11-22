"""Block 4 scoring, offline: pure code, no fakes needed."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scoring  # noqa: E402
from records import Brief, Candidate, Evidence, Signal  # noqa: E402

CONFIG = {
    "profiles": {
        "conference": {"past_sponsorships": 0.30, "audience_fit": 0.25, "active_hiring": 0.15,
                       "budget": 0.15, "city_presence": 0.10, "reachable_contact": 0.05},
    },
    "restricted_categories": ["tobacco"],
    "category_keywords": {"alcohol": ["alcohol", "wine", "spirit", "beer"], "tobacco": ["tobacco"]},
}


def evidence(verified=True):
    return Evidence(claim="c", value=1, source_url="https://example.com", quote="q",
                    record_date="2026-11-01" if verified else None,
                    retrieved_at="2026-11-22T09:00:00+00:00", source_type="test")


def signal(name, value, verified=True):
    return Signal(name=name, value=value, evidence=[evidence(verified)])


def candidate(company_id, name="Acme", signals=(), industries=()):
    return Candidate(company_id=company_id, name=name, domain=None,
                     signals=list(signals), industries=list(industries))


def brief(**overrides):
    fields = dict(event_name="TechNova Summit", profile="conference", event_date=None, city="Bengaluru",
                  audience_size=None, audience="founders", host_name="TechNova", host_website=None)
    fields.update(overrides)
    return Brief(**fields)


class ScoreTest(unittest.TestCase):
    def test_half_the_weight_unknown_gives_confidence_half_not_a_low_score(self):
        c = candidate(1, signals=[signal("past_sponsorships", 0.8), signal("active_hiring", 0.8),
                                  signal("reachable_contact", 0.8), signal("audience_fit", None)])
        out = scoring.score(c, brief(), CONFIG)
        self.assertEqual(out.confidence, 0.5)
        self.assertEqual(out.score, 0.8)

    def test_value_without_verified_evidence_counts_as_unknown(self):
        c = candidate(1, signals=[signal("past_sponsorships", 1.0), signal("budget", 0.0, verified=False)])
        out = scoring.score(c, brief(), CONFIG)
        self.assertEqual(out.confidence, 0.3)
        self.assertEqual(out.score, 1.0)

    def test_nothing_known_gives_no_score_and_zero_confidence(self):
        out = scoring.score(candidate(1, signals=[signal("budget", None)]), brief(), CONFIG)
        self.assertIsNone(out.score)
        self.assertEqual(out.confidence, 0)

    def test_bad_signals_raise(self):
        for signals in ([signal("budgett", 0.5)],
                        [signal("budget", 1.5)],
                        [signal("budget", 0.5), signal("budget", 0.6)]):
            with self.assertRaises(ValueError):
                scoring.score(candidate(1, signals=signals), brief(), CONFIG)

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            scoring.score(candidate(1), brief(profile="wedding"), CONFIG)

    def test_score_does_not_change_its_input(self):
        c = candidate(1, signals=[signal("budget", 0.5)])
        scoring.score(c, brief(), CONFIG)
        self.assertIsNone(c.score)


class FilterTest(unittest.TestCase):
    def test_current_sponsor_is_excluded_whatever_the_legal_suffix(self):
        out = scoring.score(candidate(1, name="Trainocate Networks India"),
                            brief(current_sponsors=["Trainocate Networks India Pvt Ltd"]), CONFIG)
        self.assertEqual(out.excluded_reason, "already a sponsor of this event")
        self.assertIsNone(out.score)

    def test_host_excluded_category_matches_industry_through_keywords(self):
        c = candidate(1, industries=["Wine & Spirits"], signals=[signal("budget", 1.0)])
        for wording in ["alcohol", "Alcohol brands"]:
            out = scoring.score(c, brief(excluded_categories=[wording]), CONFIG)
            self.assertIn("excluded by the host", out.excluded_reason)

    def test_category_without_keywords_matches_by_its_own_name(self):
        out = scoring.score(candidate(1, industries=["Banking"]), brief(excluded_categories=["banks"]), CONFIG)
        self.assertIn("Banking", out.excluded_reason)

    def test_competing_and_rival_wordings_match_the_category(self):
        for wording in ["competing banks", "rival banks"]:
            out = scoring.score(candidate(1, industries=["Banking"]),
                                brief(excluded_categories=[wording]), CONFIG)
            self.assertIn("Banking", out.excluded_reason, wording)

    def test_restricted_category_applies_to_every_event(self):
        out = scoring.score(candidate(1, industries=["Tobacco Manufacturing"]), brief(), CONFIG)
        self.assertIn("restricted for every event", out.excluded_reason)

    def test_filler_only_category_excludes_nothing(self):
        out = scoring.score(candidate(1, industries=["Software Development"]),
                            brief(excluded_categories=["brands"]), CONFIG)
        self.assertIsNone(out.excluded_reason)

    def test_unknown_industry_is_not_excluded(self):
        out = scoring.score(candidate(1), brief(excluded_categories=["alcohol"]), CONFIG)
        self.assertIsNone(out.excluded_reason)


class RankTest(unittest.TestCase):
    def test_order_score_then_confidence_then_id_with_excluded_kept_last(self):
        cands = [
            candidate(5, signals=[signal("budget", 0.5)]),                               # 0.5, conf 0.15
            candidate(4, signals=[signal("budget", 0.5), signal("city_presence", 0.5)]),  # 0.5, conf 0.25
            candidate(3, signals=[signal("budget", 0.9)]),                               # 0.9
            candidate(2),                                                                # unscored
            candidate(1, industries=["Tobacco"], signals=[signal("budget", 1.0)]),       # excluded
            candidate(6, signals=[signal("budget", 0.5)]),                               # ties with 5 -> id
        ]
        ranked = scoring.rank(cands, brief(), CONFIG)
        self.assertEqual([c.company_id for c in ranked], [3, 4, 5, 6, 2, 1])
        self.assertEqual(len(ranked), len(cands))

    def test_duplicate_company_is_kept_but_excluded(self):
        ranked = scoring.rank([candidate(7, signals=[signal("budget", 0.4)]), candidate(7)], brief(), CONFIG)
        self.assertEqual(ranked[0].score, 0.4)
        self.assertEqual(ranked[1].excluded_reason, "duplicate of company 7")


class RealConfigTest(unittest.TestCase):
    def test_each_profile_has_the_same_signals_and_sums_to_one(self):
        config = scoring.load_config()
        self.assertEqual(set(config["profiles"]), {"conference", "concert"})
        names = [set(w) for w in config["profiles"].values()]
        self.assertEqual(names[0], names[1])
        for weights in config["profiles"].values():
            self.assertAlmostEqual(sum(weights.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
