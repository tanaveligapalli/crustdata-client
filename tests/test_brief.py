"""Block 4 brief, offline: the model is a fake."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import brief  # noqa: E402
import scoring  # noqa: E402
from tests.test_scoring import CONFIG as SCORING_CONFIG, candidate, signal  # noqa: E402

CONFIG = {"profile_keywords": {
    "conference": ["conference", "summit", "hackathon"],
    "concert": ["concert", "music festival", "live music"],
}, "host_type_keywords": {
    "university": ["university", "college"], "ngo": ["foundation", "ngo"],
    "company": ["pvt", "ltd", "company"], "community": ["club", "community"],
}}

WORDING_A = """TechNova Summit 2027 is a two-day tech summit in Bengaluru starting 14 March 2027,
hosted by TechNova Foundation (technova.org). We expect 2,000 attendees: startup founders and engineers.
Title sponsor: ₹5 lakh, includes main stage branding. Infosys is already our title sponsor.
No alcohol brands please."""

WORDING_B = """We're TechNova Foundation and we run TechNova Summit 2027, a tech conference.
It's in Bengaluru on March 14, 2027, for around 2k people, mostly startup founders and engineers.
Our Title sponsor package costs ₹5,00,000 with main stage branding.
Current sponsor: Infosys Limited. Please avoid Alcohol."""


def model_output(**overrides):
    out = {"event_name": "TechNova Summit 2027", "event_type_quote": None, "event_date": None,
           "city": None, "audience_size_quote": None, "audience": None, "host_name": "TechNova Foundation",
           "host_type_quote": "TechNova Foundation", "host_website": None, "tiers": [], "current_sponsors": [], "past_sponsors": [],
           "excluded_categories": []}
    out.update(overrides)
    return out


class FakeModel:
    """Returns the given replies in order and counts calls."""

    def __init__(self, *replies):
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return self.replies[min(self.calls, len(self.replies)) - 1], 0.001


class BriefTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def make(self, text, *replies):
        return brief.make_brief(text, self.db, "test", CONFIG, FakeModel(*replies))

    def test_two_wordings_give_the_same_profile_and_ranking(self):
        a, _ = self.make(WORDING_A, model_output(
            event_type_quote="tech summit", event_date="2027-03-14", city="Bengaluru",
            audience_size_quote="2,000 attendees", audience="startup founders and engineers",
            host_website="technova.org", current_sponsors=["Infosys"], excluded_categories=["alcohol brands"],
            tiers=[{"name": "Title sponsor", "price_quote": "₹5 lakh", "benefits": ["main stage branding"]}]))
        b, _ = self.make(WORDING_B, model_output(
            event_type_quote="a tech conference", event_date="2027-03-14", city="Bengaluru",
            audience_size_quote="around 2k people", audience="mostly startup founders and engineers",
            current_sponsors=["Infosys Limited"], excluded_categories=["Alcohol"],
            tiers=[{"name": "Title sponsor", "price_quote": "₹5,00,000", "benefits": ["main stage branding"]}]))

        self.assertEqual((a.profile, b.profile), ("conference", "conference"))
        self.assertEqual((a.audience_size, b.audience_size), (2000, 2000))
        self.assertEqual(a.tiers[0].price_inr, b.tiers[0].price_inr)
        self.assertEqual(a.tiers[0].price_inr, 500000)

        cands = [candidate(1, name="Infosys", signals=[signal("budget", 0.9)]),
                 candidate(2, name="Bira 91", industries=["Beverage Manufacturing", "Beer"],
                           signals=[signal("budget", 0.8)]),
                 candidate(3, name="Zerodha", signals=[signal("budget", 0.6), signal("city_presence", 1.0)]),
                 candidate(4, name="Razorpay", signals=[signal("budget", 0.7)])]
        rank_a = [(c.company_id, c.excluded_reason is None) for c in scoring.rank(cands, a, SCORING_CONFIG)]
        rank_b = [(c.company_id, c.excluded_reason is None) for c in scoring.rank(cands, b, SCORING_CONFIG)]
        self.assertEqual(rank_a, rank_b)
        # "Infosys" vs "Infosys Limited" differ in wording; the excluded set must not depend on it.
        self.assertEqual(rank_a[-1], (2, False))

    def test_values_not_in_the_text_are_dropped(self):
        b, dropped = self.make(WORDING_A, model_output(
            event_type_quote="tech summit", audience="startup founders", city="Mumbai",
            current_sponsors=["Infosys", "Wipro"], host_website="https://technova.io"))
        self.assertIsNone(b.city)
        self.assertEqual(b.current_sponsors, ["Infosys"])
        self.assertIsNone(b.host_website)
        self.assertEqual({d["field"] for d in dropped}, {"city", "current_sponsors", "host_website"})

    def test_date_whose_year_is_not_written_is_dropped(self):
        text = "TechNova Summit 2027 hackathon on 14 March, for students. Hosted by TechNova Foundation."
        b, dropped = self.make(text, model_output(event_type_quote="hackathon", event_date="2026-03-14",
                                                  audience="students"))
        self.assertIsNone(b.event_date)
        self.assertIn("event_date", [d["field"] for d in dropped])

    def test_event_type_matching_both_profiles_needs_the_host(self):
        text = "TechNova Summit 2027: a conference with live music, for students. Hosted by TechNova Foundation."
        with self.assertRaises(brief.BriefIncomplete) as ctx:
            self.make(text, model_output(event_type_quote="a conference with live music", audience="students"))
        self.assertEqual(ctx.exception.missing, ["profile"])

    def test_missing_required_fields_are_listed(self):
        with self.assertRaises(brief.BriefIncomplete) as ctx:
            self.make(WORDING_A, model_output(event_name=None, host_name=None))
        self.assertEqual(ctx.exception.missing, ["event_name", "profile", "audience", "host_name"])

    def test_concert_profile(self):
        text = "Monsoon Beats 2027 is a music festival in Pune for college students, by the Monsoon music club."
        b, _ = self.make(text, model_output(event_name="Monsoon Beats 2027", event_type_quote="music festival",
                                            audience="college students", host_name="Monsoon music club",
                                            host_type_quote="music club"))
        self.assertEqual((b.profile, b.host_type), ("concert", "community"))

    def test_two_wordings_give_the_same_host_type(self):
        a, _ = self.make(WORDING_A, model_output(event_type_quote="tech summit", audience="startup founders"))
        b, _ = self.make(WORDING_B, model_output(event_type_quote="a tech conference", audience="startup founders"))
        self.assertEqual((a.host_type, b.host_type), ("ngo", "ngo"))

    def test_host_type_matching_two_kinds_is_asked(self):
        text = "TechNova Summit 2027, a tech summit for students, by TechNova Foundation, a student club."
        with self.assertRaises(brief.BriefIncomplete) as ctx:
            self.make(text, model_output(event_type_quote="tech summit", audience="students",
                                         host_type_quote="TechNova Foundation, a student club"))
        self.assertEqual(ctx.exception.missing, ["host_type"])

    def test_second_run_reads_the_cache_and_both_go_in_the_ledger(self):
        out = model_output(event_type_quote="tech summit", audience="startup founders and engineers")
        model = FakeModel(out)
        brief.make_brief(WORDING_A, self.db, "test", CONFIG, model)
        brief.make_brief(WORDING_A, self.db, "test", CONFIG, model)
        self.assertEqual(model.calls, 1)
        statuses = [r[0] for r in self.db.execute("SELECT status FROM ledger ORDER BY id")]
        self.assertEqual(statuses, ["ok", "cached"])

    def test_invalid_json_is_retried_once(self):
        good = model_output(event_type_quote="tech summit", audience="startup founders and engineers")
        b, _ = self.make(WORDING_A, "not json", good)
        self.assertEqual(b.profile, "conference")
        with self.assertRaises(brief.ExtractionFailed):
            brief.make_brief("other text", self.db, "test", CONFIG, FakeModel("nope", "{}"))


class ParseAmountTest(unittest.TestCase):
    def test_indian_and_short_forms(self):
        cases = {"₹5,00,000": 500000, "₹5 lakh": 500000, "2.5L": 250000, "1 crore": 10_000_000,
                 "1.5 Cr": 15_000_000, "5k people": 5000, "2,000 attendees": 2000, "10 lakhs + GST": 1_000_000}
        for quote, expected in cases.items():
            self.assertEqual(brief.parse_amount(quote), expected, quote)

    def test_ranges_and_no_numbers_give_none(self):
        for quote in ["2,000-3,000 people", "₹5 lakh for 2 days", "a lot of people"]:
            self.assertIsNone(brief.parse_amount(quote), quote)


class RealConfigTest(unittest.TestCase):
    def test_profile_keywords_cover_both_profiles_without_overlap(self):
        keywords = brief.load_config()["profile_keywords"]
        self.assertEqual(set(keywords), {"conference", "concert"})
        self.assertFalse(set(keywords["conference"]) & set(keywords["concert"]))

    def test_host_type_keywords_match_the_weight_tables_without_overlap(self):
        keywords = brief.load_config()["host_type_keywords"]
        self.assertEqual(set(keywords), set(scoring.load_config()["weights"]))
        words = [w for kind in keywords.values() for w in kind]
        self.assertEqual(len(words), len(set(words)))


if __name__ == "__main__":
    unittest.main()
