"""Host verification, offline: the web fetch and company lookup are fakes."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import evidence  # noqa: E402
import host_check  # noqa: E402
from crustdata_client import ApiResponse  # noqa: E402
from records import Brief  # noqa: E402

RETRIEVED = "2026-11-22T10:00:00+00:00"


def brief(website="technova.org"):
    return Brief(event_name="TechNova Summit 2027", profile="conference", event_date="2027-03-14",
                 city="Bengaluru", audience_size=2000, audience="founders", host_name="TechNova Foundation",
                 host_website=website)


class FakeClient:
    def __init__(self, page=None, identify=None, timestamp=1795000000):
        self.page, self.identify, self.paths, self.timestamp = page, identify or [], [], timestamp

    def call(self, path, body):
        self.paths.append(path)
        if path == "/web/enrich/live":
            url = body["urls"][0]
            if self.page is None:
                data = [{"url": url, "success": False, "content": None}]
            else:
                data = [{"url": url, "success": True, "content": self.page, "timestamp": self.timestamp}]
            return ApiResponse(data, RETRIEVED, False, 1)
        return ApiResponse(self.identify, RETRIEVED, False, 0)


PAGE = "<html><body><h1>TechNova Foundation</h1><p>We run student tech events.</p></body></html>"
IDENTIFY = [{"matches": [{"company_data": {"crustdata_company_id": 42,
                                          "basic_info": {"primary_domain": "technova.org"}}}]}]


class HostCheckTests(unittest.TestCase):
    def test_site_that_names_the_host_is_verified(self):
        fact = host_check.check_host(brief(), FakeClient(PAGE, IDENTIFY))
        self.assertEqual(fact.status, "verified")
        self.assertEqual(fact.source_url, "https://technova.org")
        self.assertEqual(fact.value["crustdata_company_id"], 42)
        self.assertIsNotNone(fact.record_date)

    def test_unknown_to_crustdata_is_still_verified_by_the_site(self):
        fact = host_check.check_host(brief(), FakeClient(PAGE))
        self.assertEqual(fact.status, "verified")
        self.assertIsNone(fact.value["crustdata_company_id"])

    def test_page_with_no_date_says_so_not_that_the_host_is_missing(self):
        fact = host_check.check_host(brief(), FakeClient(PAGE, timestamp=None))
        self.assertEqual(fact.status, "cannot_verify")
        self.assertEqual(fact.missing, "the record has no date")

    def test_site_about_someone_else_is_not_verified(self):
        fact = host_check.check_host(brief(), FakeClient("<html>Acme Plumbing, since 1990</html>"))
        self.assertEqual(fact.status, "cannot_verify")
        self.assertIn("does not mention", fact.missing)

    def test_dead_site_is_not_verified(self):
        fact = host_check.check_host(brief(), FakeClient(None))
        self.assertEqual(fact.status, "cannot_verify")
        self.assertIn("could not be fetched", fact.missing)

    def test_no_website_means_no_call_and_no_verification(self):
        client = FakeClient(PAGE)
        fact = host_check.check_host(brief(None), client)
        self.assertEqual(fact.status, "cannot_verify")
        self.assertEqual(client.paths, [])

    def test_unusable_address_is_not_verified(self):
        fact = host_check.check_host(brief("not a website"), FakeClient(PAGE))
        self.assertEqual(fact.status, "cannot_verify")

    def test_domain_of(self):
        self.assertEqual(host_check.domain_of("https://www.TechNova.org/about"), "technova.org")
        self.assertEqual(host_check.domain_of("technova.org"), "technova.org")
        self.assertIsNone(host_check.domain_of("localhost"))

    def test_result_is_saved_for_the_run(self):
        db = agent_db.connect(":memory:")
        host_check.check_host(brief(), FakeClient(PAGE), db, "run-1")
        saved = evidence.load(db, "run-1", "host:TechNova Foundation")
        self.assertEqual([f.status for f in saved], ["verified"])
        db.close()


class IdentifyShapeTest(unittest.TestCase):
    def test_a_match_with_no_domain_is_skipped_not_a_crash(self):
        response = [{"matches": [{"company_data": {"crustdata_company_id": 1, "basic_info": {"primary_domain": None}}}]}]
        self.assertIsNone(host_check.crustdata_company_id(response, "example.com"))


if __name__ == "__main__":
    unittest.main()
