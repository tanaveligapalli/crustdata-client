"""Block 3, offline: candidate pools with a fake Crustdata client and a fake model."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import candidates  # noqa: E402
import evidence  # noqa: E402
from candidates import Hit  # noqa: E402
from crustdata_client import ApiResponse, SourceUnavailable  # noqa: E402
from records import Brief  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RETRIEVED = "2026-11-22T09:00:00+00:00"
CFG = candidates.load_config()
IG_CONFIG = {"match": {"min_confidence": 0.8, "ambiguity_margin": 0.05, "identify_batch_size": 25}}
COMPANY_IDS = {"Sunalco": 42, "LanceSoft": 1266602}  # what the fake /company/identify knows


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def brief(**overrides):
    fields = dict(event_name="FinTech Forward Summit", profile="conference", event_date="2027-02-20",
                  city="Noida", audience_size=800, audience="fintech leaders", host_name="FinTech Forward",
                  host_website=None)
    fields.update(overrides)
    return Brief(**fields)


class FakeClient:
    """Answers each endpoint from a fixture or a function; a path in `failing` keeps failing."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.paths = []

    def call(self, path, body):
        self.paths.append(path)
        if path in self.failing:
            raise SourceUnavailable(503, "down", path)
        return ApiResponse(self.answer(path, body), RETRIEVED, from_cache=False, credits=0)

    def request(self, path, body):
        return self.call(path, body).data

    def answer(self, path, body):
        if path == "/job/search":
            return fixture("job_search_sponsor_roles.json")
        if path == "/social_post/search":
            return fixture("social_post_search_sponsors.json")
        if path == "/company/identify":
            return [{"matched_on": name, "matches": [
                {"confidence_score": 1.0, "company_data": {"crustdata_company_id": COMPANY_IDS[name],
                                                           "basic_info": {"name": name}}}]}
                for name in body["names"] if name in COMPANY_IDS]
        if path == "/company/search":
            return {"companies": [{"crustdata_company_id": 7, "basic_info": {"name": "Acme Bank"},
                                   "taxonomy": {"professional_network_industries": ["Banking"]}}]}
        if path == "/web/search/live":
            return {"results": []}
        raise AssertionError(f"unexpected call to {path}")


def fake_model(system, user):
    """Reads the two fixture posts the way the real model should."""
    mentions = []
    if "welcome Sunalco" in user:
        mentions = [{"name": "Sunalco", "role": "gifting",
                     "evidence_span": "welcome Sunalco as the Gift Sponsor"},
                    {"name": "Sunalco Alloys Private Limited", "role": "gifting",
                     "evidence_span": "Sunalco Alloys Private Limited"}]
    if "As a Gold Sponsor, LanceSoft" in user:
        mentions = [{"name": "LanceSoft", "role": "other",
                     "evidence_span": "As a Gold Sponsor, LanceSoft was proud"}]
    return json.dumps({"event_name": None, "event_dates": [], "city": None, "organizer_handle": "x",
                       "sponsor_mentions": mentions, "sponsor_ask": False, "contact_hint": None}), 0.001


class HiringPoolTest(unittest.TestCase):
    def test_each_posting_is_a_dated_fact_from_its_job_url(self):
        hits = candidates.hiring_pool(brief(city=None), FakeClient(), CFG)

        biggbell = [h for h in hits if h.company_id == 2157523]
        self.assertEqual(len(biggbell), 2)
        fact = biggbell[0].facts[0]
        self.assertEqual(fact.status, "verified")
        self.assertEqual(fact.source_url, "https://www.linkedin.com/jobs/view/4491640670")
        self.assertEqual(fact.record_date, "2026-11-19T19:24:11")
        self.assertEqual(biggbell[0].signal, "active_hiring")

    def test_a_posting_in_the_event_city_is_a_city_presence_fact(self):
        hits = candidates.hiring_pool(brief(city="noida"), FakeClient(), CFG)
        city_ids = {h.company_id for h in hits if h.signal == "city_presence"}
        self.assertEqual(city_ids, {2157523, 21121})  # Biggbell and LoansJagat post in Noida


class PostPoolTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_linkedin_sponsors_resolve_to_companies_with_the_post_date(self):
        hits = candidates.linkedin_sponsor_pool(brief(), FakeClient(), self.db, "run-1", CFG,
                                                IG_CONFIG, fake_model)

        self.assertEqual(sorted(h.company_id for h in hits), [42, 1266602])
        fact = next(h for h in hits if h.company_id == 42).facts[0]
        self.assertEqual(fact.status, "verified")
        self.assertEqual(fact.source_type, "linkedin_post")
        self.assertEqual(fact.quote, "welcome Sunalco as the Gift Sponsor")
        self.assertTrue(fact.record_date.startswith("2026-09-16"))

    def test_a_brand_with_no_company_record_is_kept_as_cannot_verify(self):
        candidates.linkedin_sponsor_pool(brief(), FakeClient(), self.db, "run-1", CFG, IG_CONFIG, fake_model)

        unresolved = evidence.load(self.db, "run-1", "unresolved")
        self.assertEqual([f.value for f in unresolved], ["Sunalco Alloys Private Limited"])
        self.assertEqual(unresolved[0].status, "cannot_verify")


class MergeTest(unittest.TestCase):
    def fact(self, url, status="verified"):
        return evidence.from_page(url, "Gold sponsor: Acme", "Gold sponsor: Acme" if status == "verified"
                                  else "not on page", "Acme sponsored X", "Gold", "2026-09-04", RETRIEVED)

    def test_two_pools_finding_one_company_give_one_candidate(self):
        hits = [Hit(7, "Acme", "hiring", industries=["Banking"]), Hit(7, "Acme", "company_search"),
                Hit(8, "Beta", "company_search")]
        merged = candidates.merge(hits, brief(), CFG)
        self.assertEqual([c.company_id for c in merged], [7, 8])
        self.assertEqual(merged[0].found_by, ["hiring", "company_search"])
        self.assertEqual(merged[0].industries, ["Banking"])

    def test_signal_value_counts_verified_sources(self):
        hits = [Hit(7, "Acme", "past_sponsor", "past_sponsorships", [self.fact("https://a.in/1")]),
                Hit(7, "Acme", "past_sponsor", "past_sponsorships", [self.fact("https://a.in/1")])]
        signal = candidates.merge(hits, brief(), CFG)[0].signals[0]
        self.assertEqual(signal.value, 0.5)  # one distinct source; 2 make it 1.0
        self.assertEqual(len(signal.evidence), 2)

    def test_only_unverified_facts_leave_the_signal_unknown(self):
        hits = [Hit(7, "Acme", "past_sponsor", "past_sponsorships", [self.fact("https://a.in/1", "no")])]
        signal = candidates.merge(hits, brief(), CFG)[0].signals[0]
        self.assertIsNone(signal.value)
        self.assertEqual(signal.evidence[0].status, "cannot_verify")

    def test_the_host_is_never_a_candidate(self):
        hits = [Hit(9, "FinTech Forward Pvt Ltd", "company_search")]
        self.assertEqual(candidates.merge(hits, brief(), CFG), [])


class FindCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_full_run_merges_pools_and_saves_facts(self):
        found, failed = candidates.find_candidates(brief(past_sponsors=["LanceSoft"]), FakeClient(),
                                                   self.db, "run-1", CFG, IG_CONFIG, fake_model)
        self.assertEqual(failed, [])
        ids = [c.company_id for c in found]
        self.assertEqual(len(ids), len(set(ids)))
        lancesoft = next(c for c in found if c.company_id == 1266602)
        self.assertEqual(lancesoft.found_by, ["host_past_sponsor", "past_sponsor"])
        self.assertTrue(evidence.load(self.db, "run-1", "company:2157523"))

    def test_a_failing_source_is_reported_and_the_rest_still_run(self):
        found, failed = candidates.find_candidates(brief(), FakeClient(failing={"/job/search"}),
                                                   self.db, "run-1", CFG, IG_CONFIG, fake_model)
        self.assertEqual(failed, ["hiring"])
        self.assertIn(7, [c.company_id for c in found])

    def test_without_a_model_the_post_pools_are_reported_not_skipped_silently(self):
        _, failed = candidates.find_candidates(brief(), FakeClient(), self.db, "run-1", CFG, IG_CONFIG, None)
        self.assertEqual(failed, ["linkedin_posts (no model)", "instagram_posts (no model)"])


if __name__ == "__main__":
    unittest.main()
