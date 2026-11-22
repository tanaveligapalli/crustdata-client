"""Company signals, offline: captured Crustdata responses and synthetic pages, no live calls."""

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_signals as cs  # noqa: E402
from crustdata_client import ApiResponse  # noqa: E402
from records import Candidate  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RETRIEVED = "2026-11-22T09:00:00+00:00"
CFG = {"company_size_full_at": 10000, "followers_full_at": 1000000, "growth_span_percent": 20,
       "funding_months": [12, 24], "alumni_full_at": 20}

CSR_HOME = '<html><a href="/about/careers">Careers</a><a href="/about-us/corporate-social-responsibility">Our CSR</a></html>'
CSR_PAGE = ("<html><body><h1>Corporate Social Responsibility</h1><p>Acme Limited supports rural schools. "
            "CSR Programmes approved by the Board for FY 2026-27 include digital literacy.</p></body></html>")
CSR_PAGE_NO_YEAR = "<html><body><h1>CSR</h1><p>Acme Limited supports rural schools.</p></body></html>"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.paths = []

    def call(self, path, body):
        self.paths.append(path)
        if path == "/company/search":
            data = fixture("company_search_signals.json")
        elif path == "/person/search":
            data = fixture("person_search_alumni.json")
        else:
            url = body["urls"][0]
            content = self.pages.get(url)
            data = [{"url": url, "success": content is not None, "content": content}]
        return ApiResponse(data, RETRIEVED, from_cache=False, credits=0)


class ValueTest(unittest.TestCase):
    def test_log_scales(self):
        self.assertEqual(cs.log_scale(100, 10000), 0.5)
        self.assertEqual(cs.log_scale(50000, 10000), 1.0)
        self.assertEqual(cs.log_scale(0, 10000), 0.0)

    def test_layoffs_lower_growth(self):
        self.assertEqual(cs.growth_value(-20, 20), 0.0)
        self.assertEqual(cs.growth_value(0, 20), 0.5)
        self.assertEqual(cs.growth_value(35, 20), 1.0)

    def test_recent_funding_counts_most(self):
        today = date(2026, 11, 22)
        self.assertEqual(cs.funding_value(date(2026, 8, 19), today, [12, 24]), 1.0)
        self.assertEqual(cs.funding_value(date(2025, 3, 1), today, [12, 24]), 0.5)
        self.assertEqual(cs.funding_value(date(2022, 5, 10), today, [12, 24]), 0.1)


class CompanyFactsTest(unittest.TestCase):
    def setUp(self):
        self.signals = cs.company_facts([2289, 5708, 897127], FakeClient(), CFG, today=date(2026, 11, 22))

    def by_name(self, company_id):
        return {s.name: s for s in self.signals[company_id]}

    def test_listed_company(self):
        itc = self.by_name(2289)
        self.assertEqual(itc["listed"].value, 1.0)
        self.assertIn("ITC", itc["listed"].evidence[0].claim)
        self.assertEqual(itc["company_size"].value, 1.0)          # 63,554 staff
        self.assertNotIn("funding", itc)                           # post-IPO secondary: no new money

    def test_private_company_is_known_not_listed(self):
        self.assertEqual(self.by_name(897127)["listed"].value, 0.0)

    def test_recent_round_and_secondary_sale(self):
        self.assertEqual(self.by_name(5708)["funding"].value, 1.0)               # Navi, Aug 2026
        self.assertEqual(self.by_name(5708)["funding"].evidence[0].record_date, "2026-08-19")
        self.assertNotIn("funding", self.by_name(897127))                        # Razorpay: secondary market

    def test_every_fact_is_sourced_and_dated(self):
        for signals in self.signals.values():
            for s in signals:
                fact = s.evidence[0]
                self.assertEqual(fact.status, "verified", fact.claim)
                self.assertTrue(fact.source_url.startswith("https://www.linkedin.com/company/"), fact.source_url)
                self.assertTrue(0 <= s.value <= 1)


class AlumniTest(unittest.TestCase):
    def test_count_becomes_a_dated_fact(self):
        razorpay = Candidate(897127, "Razorpay", "razorpay.com")
        signal = cs.alumni([razorpay], "BITS Pilani", FakeClient(), CFG)[897127][0]
        self.assertEqual(signal.value, 0.5)                        # 10 of 20
        self.assertEqual(signal.evidence[0].quote, "total_count = 10")
        self.assertEqual(signal.evidence[0].record_date, "2026-11-22")
        self.assertEqual(signal.evidence[0].status, "verified")


class CsrTest(unittest.TestCase):
    ACME = Candidate(1, "Acme Limited", "acme.in")

    def run_csr(self, page):
        pages = {"https://www.acme.in": CSR_HOME,
                 "https://www.acme.in/about-us/corporate-social-responsibility": page}
        return cs.csr([self.ACME], FakeClient(pages)).get(1)

    def test_csr_page_with_a_financial_year_is_verified_from_its_start(self):
        signal = self.run_csr(CSR_PAGE)[0]
        self.assertEqual(signal.value, 1.0)
        self.assertEqual(signal.evidence[0].status, "verified")
        self.assertEqual(signal.evidence[0].record_date, "2026-04-01")
        self.assertIn("FY 2026-27", signal.evidence[0].quote)

    def test_csr_page_without_a_year_cannot_verify(self):
        signal = self.run_csr(CSR_PAGE_NO_YEAR)[0]
        self.assertIsNone(signal.value)
        self.assertEqual(signal.evidence[0].missing, "the CSR page names no financial year")

    def test_no_csr_link_means_no_signal(self):
        pages = {"https://www.acme.in": '<a href="/careers">Careers</a>'}
        self.assertIsNone(cs.csr([self.ACME], FakeClient(pages)).get(1))

    def test_links_resolve_against_the_site(self):
        self.assertEqual(cs.csr_link('<a href="/csr-policy.html">Policy</a>', "https://www.acme.in"),
                         "https://www.acme.in/csr-policy.html")
        self.assertEqual(cs.csr_link('<a href="https://acme.in/x">Corporate Social Responsibility</a>', "x"),
                         "https://acme.in/x")
        self.assertIsNone(cs.csr_link('<a href="/ecsr-tools">Tools</a>', "https://www.acme.in"))


if __name__ == "__main__":
    unittest.main()
