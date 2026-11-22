import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from instagram_parser import parse_web_fetch_response  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "web_fetch.json"


class TestInstagramParser(unittest.TestCase):
    def setUp(self):
        self.result = parse_web_fetch_response(json.loads(FIXTURE.read_text()))[0]

    def test_handle(self):
        self.assertEqual(self.result["handle"], "incubationmasters")

    def test_caption(self):
        self.assertTrue(self.result["caption"].startswith("Last Hour Left!"))
        self.assertIn("https://hackmake.in/", self.result["caption"])

    def test_dates(self):
        self.assertEqual(self.result["shortcode"], "DTNUCNQCL_w")
        self.assertEqual(self.result["taken_at"], 1767785782)
        # Not present in this fixture.
        self.assertIsNone(self.result["datePublished"])
        self.assertIsNone(self.result["uploadDate"])


if __name__ == "__main__":
    unittest.main()
