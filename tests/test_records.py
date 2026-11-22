import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from records import Candidate, Evidence, Signal  # noqa: E402


def evidence(record_date):
    return Evidence(
        claim="ITC Limited was Munching Partner at SIDTM Hackathon 2026",
        value="Munching Partner",
        source_url="https://www.instagram.com/p/Dc3Hy3qjeRC/",
        quote="Munching Partner: ITC Limited",
        record_date=record_date,
        retrieved_at="2026-11-22T08:00:00+00:00",
        source_type="instagram",
    )


class RecordsTest(unittest.TestCase):
    def test_dated_evidence_is_verified(self):
        self.assertEqual(evidence("2026-09-04").status, "verified")

    def test_undated_evidence_cannot_verify(self):
        undated = evidence(None)
        self.assertEqual(undated.status, "cannot_verify")
        self.assertEqual(undated.missing, "the record has no date")

    def test_records_turn_into_plain_dicts(self):
        candidate = Candidate(company_id=1, name="ITC Limited", domain="itcportal.com",
                              signals=[Signal("past_sponsorships", 1.0, [evidence("2026-09-04")])])
        self.assertEqual(asdict(candidate)["signals"][0]["evidence"][0]["status"], "verified")


if __name__ == "__main__":
    unittest.main()
