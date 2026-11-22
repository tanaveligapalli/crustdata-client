import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from instagram_parser import parse_post, parse_web_fetch_response  # noqa: E402

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

    def test_status_and_time_check(self):
        self.assertEqual(self.result["kind"], "post")
        self.assertEqual(self.result["parse_status"], "ok")
        self.assertEqual(self.result["comment_count"], 11)
        self.assertEqual(self.result["time_tag"], "2026-01-07T11:36:25.000Z")
        self.assertEqual(self.result["time_delta_seconds"], 3)
        self.assertFalse(self.result["time_disagrees"])


def _page(obj):
    return '<script type="application/json">' + json.dumps(obj) + "</script>"


MEDIA = {"code": "AAA", "taken_at": 1000, "caption": {"text": "hi"}, "user": {"username": "u"}}


class TestParseStatus(unittest.TestCase):
    """Synthetic pages: these test the status logic, not Instagram's format."""

    def test_gated(self):
        r = parse_post(_page({"if_not_gated_logged_out": None}), "https://www.instagram.com/p/AAA/")
        self.assertEqual(r["parse_status"], "gated")

    def test_shortcode_mismatch(self):
        r = parse_post(_page({"x": MEDIA}), "https://www.instagram.com/reel/BBB/")
        self.assertEqual((r["kind"], r["parse_status"]), ("reel", "shortcode_mismatch"))

    def test_no_post_object(self):
        r = parse_post("<html></html>", "https://www.instagram.com/p/AAA/")
        self.assertEqual(r["parse_status"], "no_post_object")

    def test_time_disagreement_flagged(self):
        html = _page({"m": MEDIA}) + '<time datetime="1970-01-01T00:20:00.000Z"></time><span>hi</span>'
        r = parse_post(html, "https://www.instagram.com/p/AAA/")
        self.assertEqual(r["time_delta_seconds"], 200)
        self.assertTrue(r["time_disagrees"])


if __name__ == "__main__":
    unittest.main()
