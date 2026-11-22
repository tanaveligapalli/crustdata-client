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
COMPANY_IDS = {"LanceSoft": 1266602, "FICCI": 77, "The Forum": 55}  # what the fake /company/identify knows
# Company IDs in the post fixture: Sunalco is tagged in the first post, LanceSoft wrote the second.
COMPANY_NAMES = {64995777: "Sunalco Alloys Private Limited", 1266602: "LanceSoft India",
                 4316132: "Bureau of Middle East Recycling"}


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
        self.bodies = []

    def call(self, path, body):
        self.paths.append(path)
        self.bodies.append(body)
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
        if path == "/company/search" and body["filters"].get("field") == "crustdata_company_id":
            return {"companies": [{"crustdata_company_id": i, "basic_info": {"name": COMPANY_NAMES[i]}}
                                  for i in body["filters"]["value"] if i in COMPANY_NAMES]}
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
                     "evidence_span": "Sunalco Alloys Private Limited"},
                    {"name": "The Forum", "role": "venue",
                     "evidence_span": "The Forum- Hotel & Convention"}]
    if "As a Gold Sponsor, LanceSoft" in user:
        mentions = [{"name": "LanceSoft", "role": "other",
                     "evidence_span": "As a Gold Sponsor, LanceSoft was proud"},
                    {"name": "FICCI", "role": "other", "evidence_span": "FICCI"},
                    {"name": "BRICS", "role": "other", "evidence_span": "BRICS Conference 2026"}]
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

        self.assertEqual(sorted({h.company_id for h in hits}), [77, 1266602, 64995777])
        fact = next(h for h in hits if h.name == "Sunalco").facts[0]
        self.assertEqual(fact.status, "verified")
        self.assertEqual(fact.source_type, "linkedin_post")
        self.assertEqual(fact.quote, "welcome Sunalco as the Gift Sponsor")
        self.assertTrue(fact.record_date.startswith("2026-09-16"))

    def test_a_tagged_company_or_the_author_resolves_without_a_name_search(self):
        hits = candidates.linkedin_sponsor_pool(brief(), FakeClient(), self.db, "run-1", CFG,
                                                IG_CONFIG, fake_model)
        how = {h.name: h.facts[0].value["matched_by"] for h in hits}
        self.assertEqual(how["Sunalco"], "tagged in the post")      # tagged in the post
        self.assertEqual(how["LanceSoft"], "tagged in the post")    # the post's author
        self.assertEqual(how["FICCI"], "company_identify")

    def test_untagged_names_are_looked_up_once_for_all_posts(self):
        client = FakeClient()
        candidates.linkedin_sponsor_pool(brief(), client, self.db, "run-1", CFG, IG_CONFIG, fake_model)
        lookups = [b["names"] for path, b in zip(client.paths, client.bodies) if path == "/company/identify"]
        self.assertEqual(lookups, [["BRICS", "brics", "FICCI", "ficci", "The Forum", "the forum"]])

    def test_a_venue_is_not_counted_as_a_sponsor(self):
        hits = candidates.linkedin_sponsor_pool(brief(), FakeClient(), self.db, "run-1", CFG,
                                                IG_CONFIG, fake_model)
        self.assertNotIn(55, [h.company_id for h in hits])

    def test_a_brand_with_no_company_record_is_kept_as_cannot_verify(self):
        candidates.linkedin_sponsor_pool(brief(), FakeClient(), self.db, "run-1", CFG, IG_CONFIG, fake_model)

        unresolved = evidence.load(self.db, "run-1", "unresolved")
        self.assertEqual([f.value for f in unresolved], ["BRICS"])
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

    def test_many_posts_by_one_organizer_count_once(self):
        def post_fact(url, organizer):
            fact = self.fact(url)
            fact.value = {"organizer_handle": organizer, "role": "title"}
            return fact

        hits = [Hit(7, "Acme", "past_sponsor", "past_sponsorships", [post_fact(f"https://a.in/{i}", "SmartMeter")])
                for i in range(8)]
        hits.append(Hit(7, "Acme", "past_sponsor", "past_sponsorships", [post_fact("https://b.in/1", "ISODA")]))
        self.assertEqual(candidates.merge(hits, brief(), CFG)[0].signals[0].value, 1.0)
        self.assertEqual(candidates.merge(hits[:8], brief(), CFG)[0].signals[0].value, 0.5)

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
