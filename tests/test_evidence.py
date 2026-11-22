"""The evidence ledger: facts need the passage on the page and a date; nothing is dropped."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import evidence  # noqa: E402
from crustdata_client import ApiResponse  # noqa: E402
from instagram_parser import parse_post  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RETRIEVED = "2026-11-22T08:00:00+00:00"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ApiEvidenceTest(unittest.TestCase):
    def test_job_posting_field_becomes_a_dated_fact(self):
        job = fixture("job_search.json")["job_listings"][0]
        response = ApiResponse({"job_listings": [job]}, RETRIEVED, from_cache=True, credits=0)

        fact = evidence.from_api(
            "/job/search", response, "job_listings[0].job_details.title", job["job_details"]["title"],
            claim="The company is hiring a Custom Software Engineer",
            record_date=job["metadata"]["date_added"], source_url=job["job_details"]["url"],
        )

        self.assertEqual(fact.status, "verified")
        self.assertEqual(fact.quote, 'job_listings[0].job_details.title = "Custom Software Engineer"')
        self.assertEqual(fact.source_url, "https://www.linkedin.com/jobs/view/4492891970")
        self.assertEqual(fact.retrieved_at, RETRIEVED)  # a cache hit keeps the original date
        self.assertEqual(fact.source_type, "crustdata:/job/search")

    def test_api_field_without_a_date_cannot_verify(self):
        response = ApiResponse({}, RETRIEVED, from_cache=False, credits=1)
        fact = evidence.from_api("/company/search", response, "headcount", 500, "Has 500 staff", None)
        self.assertEqual(fact.status, "cannot_verify")
        self.assertEqual(fact.missing, "the record has no date")
        self.assertEqual(fact.source_url, "https://api.crustdata.com/company/search")


class PageEvidenceTest(unittest.TestCase):
    PAGE = "<html><body><h2>Our partners</h2><p>Munching&nbsp;Partner:\n  ITC   Limited</p></body></html>"

    def page_fact(self, quote, record_date="2026-09-04"):
        return evidence.from_page("https://example.in/sponsors", evidence.page_text(self.PAGE), quote,
                                  "ITC Limited was Munching Partner", "Munching Partner",
                                  record_date, RETRIEVED)

    def test_quote_on_the_page_is_verified_despite_spacing_case_and_entities(self):
        self.assertEqual(self.page_fact("munching partner: ITC Limited").status, "verified")

    def test_quote_not_on_the_page_cannot_verify(self):
        fact = self.page_fact("Title Partner: ITC Limited")
        self.assertEqual(fact.status, "cannot_verify")
        self.assertEqual(fact.missing, "the fetched page does not contain the quoted passage")

    def test_quote_that_spans_tags_is_found(self):
        self.assertEqual(self.page_fact("Our partners Munching Partner").status, "verified")

    def test_empty_quote_is_never_verified(self):
        self.assertEqual(self.page_fact("   ").status, "cannot_verify")

    def test_page_without_a_date_cannot_verify(self):
        self.assertEqual(self.page_fact("Munching Partner: ITC Limited", record_date=None).status,
                         "cannot_verify")

    def test_unreadable_date_cannot_verify(self):
        fact = self.page_fact("Munching Partner: ITC Limited", record_date="2w")
        self.assertEqual(fact.status, "cannot_verify")
        self.assertIn("not an ISO date", fact.missing)

    def test_script_text_does_not_count_as_page_text(self):
        page = "<script>var s = 'Title Partner: Acme';</script><p>Welcome</p>"
        self.assertFalse(evidence.quote_on_page("Title Partner: Acme", evidence.page_text(page)))


class WebFetchAndSnippetTest(unittest.TestCase):
    def test_instagram_caption_with_post_date_is_verified(self):
        result = fixture("web_fetch.json")[0]
        post = parse_post(result["content"], result["url"])

        fact = evidence.from_page(result["url"], post["caption"], "Registrations for Hack & Make 2026",
                                  "Hack & Make 2026 took registrations", "Hack & Make 2026",
                                  evidence.unix_to_iso(post["taken_at"]), RETRIEVED, "instagram")

        self.assertEqual(fact.status, "verified")
        self.assertEqual(fact.record_date, "2026-01-07T11:36:22+00:00")

    def test_failed_fetch_cannot_verify(self):
        result = {"url": "https://example.in/x", "success": False, "content": None}
        fact = evidence.from_web_fetch(result, RETRIEVED, "anything", "A claim", None, "2026-09-04")
        self.assertEqual(fact.status, "cannot_verify")
        self.assertEqual(fact.missing, "the page could not be fetched")

    def test_snippet_is_a_lead_not_a_fact(self):
        result = fixture("web_search.json")["results"][1]
        fact = evidence.from_snippet(result["url"], result["snippet"],
                                     "GDG GTBIT sought sponsors for HACKWAVE", "HACKWAVE", RETRIEVED)
        self.assertEqual(fact.status, "cannot_verify")
        self.assertEqual(fact.quote, result["snippet"])
        self.assertIn("fetch the page", fact.missing)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_verified_and_unverified_facts_are_both_kept(self):
        verified = evidence.from_page("https://a.in", "Gold sponsor: Acme", "Gold sponsor: Acme",
                                      "Acme was gold sponsor", {"tier": "Gold"}, "2026-09-04", RETRIEVED)
        unverified = evidence.cannot_verify("Acme sponsored DevFest", "only a logo on the page")
        evidence.save(self.db, "run-1", "company:7", [verified, unverified])

        loaded = evidence.load(self.db, "run-1", "company:7")

        self.assertEqual(loaded, [verified, unverified])
        self.assertEqual(loaded[0].value, {"tier": "Gold"})

    def test_saving_again_after_a_restart_does_not_duplicate(self):
        fact = evidence.cannot_verify("Acme sponsored DevFest", "only a logo on the page")
        evidence.save(self.db, "run-1", "company:7", fact)
        evidence.save(self.db, "run-1", "company:7", fact)
        self.assertEqual(len(evidence.load(self.db, "run-1")), 1)

    def test_load_filters_by_run_and_subject(self):
        fact = evidence.cannot_verify("A claim", "nothing found")
        evidence.save(self.db, "run-1", "company:7", fact)
        evidence.save(self.db, "run-1", "host", fact)
        evidence.save(self.db, "run-2", "company:7", fact)
        self.assertEqual(len(evidence.load(self.db, "run-1")), 2)
        self.assertEqual(len(evidence.load(self.db, "run-1", "host")), 1)


if __name__ == "__main__":
    unittest.main()
