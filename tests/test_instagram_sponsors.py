"""Block 3b, offline: the model and Crustdata are both fakes."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import instagram_sponsors as ig  # noqa: E402
from instagram_parser import parse_web_fetch_response  # noqa: E402
from src import extract, match  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
CONFIG = {"match": {"min_confidence": 0.8, "ambiguity_margin": 0.05, "identify_batch_size": 25}}
FETCHED_AT = "2026-11-22T09:00:00+00:00"


def model_output(**overrides):
    out = {"event_name": None, "event_dates": [], "city": None, "organizer_handle": "someone",
           "sponsor_mentions": [], "sponsor_ask": False, "contact_hint": None}
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


class FakeIdentify:
    """Stands in for CrustdataClient: /company/identify by names, shaped like the fixture."""

    def __init__(self, matches_by_query):
        self.matches_by_query = {k.lower(): v for k, v in matches_by_query.items()}
        self.bodies = []

    def request(self, path, body):
        assert path == "/company/identify" and list(body) == ["names"]
        self.bodies.append(body)
        return [{"matched_on": q, "match_type": "name", "matches": self.matches_by_query[q.lower()]}
                for q in body["names"] if q.lower() in self.matches_by_query]


def company(company_id, name, score):
    return {"confidence_score": score,
            "company_data": {"crustdata_company_id": company_id, "basic_info": {"name": name}}}


def post(caption, shortcode="TEST1", taken_at=1767785782):
    return {"url": f"https://www.instagram.com/p/{shortcode}/", "shortcode": shortcode, "handle": "someone",
            "caption": caption, "taken_at": taken_at, "parse_status": "ok"}


class BlockTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def run_post(self, p, model, client=None):
        return ig.process_post(p, FETCHED_AT, self.db, "test", client or FakeIdentify({}), CONFIG, model)

    def test_incubationmasters_fixture_has_no_sponsors(self):
        response = json.loads((FIXTURES / "web_fetch.json").read_text(encoding="utf-8"))
        p = parse_web_fetch_response(response)[0]
        self.assertEqual(p["parse_status"], "ok")
        # Even if the model offers the hashtag #MPStartupSummit as a sponsor, it is dropped:
        # it appears only as a hashtag, never in the caption text.
        model = FakeModel(model_output(sponsor_mentions=[
            {"name": "MPStartupSummit", "role": "other", "evidence_span": "#MPStartupSummit"}]))
        result = self.run_post(p, model)
        self.assertEqual(result["extracted"]["sponsor_mentions"], [])
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["extracted"]["organizer_handle"], "incubationmasters")

    def test_one_sponsor_becomes_evidence(self):
        caption = "Thank you ITC Limited for being our Munching Partner at SIDTM Hackathon 2026!"
        model = FakeModel(model_output(event_name="SIDTM Hackathon 2026", sponsor_mentions=[
            {"name": "ITC Limited", "role": "food_beverage",
             "evidence_span": "ITC Limited for being our Munching Partner"}]))
        client = FakeIdentify({"ITC Limited": [company(101, "ITC Limited", 0.95)]})
        result = self.run_post(post(caption), model, client)

        self.assertEqual(len(result["evidence"]), 1)
        e = result["evidence"][0]
        self.assertEqual(e.claim, "ITC Limited sponsored SIDTM Hackathon 2026 as food_beverage")
        self.assertEqual(e.value["crustdata_company_id"], 101)
        self.assertEqual(e.value["date_confidence"], "exact")
        self.assertEqual(e.quote, "ITC Limited for being our Munching Partner")
        self.assertEqual(e.record_date, "2026-01-07T11:36:22+00:00")   # from taken_at
        self.assertEqual(e.retrieved_at, FETCHED_AT)
        self.assertEqual(e.source_type, "instagram_post")
        self.assertEqual(e.status, "verified")
        self.assertEqual(len(result["saved_evidence_keys"]), 1)

    def test_span_not_in_caption_is_dropped(self):
        caption = "Thank you ITC Limited for the snacks!"
        model = FakeModel(model_output(sponsor_mentions=[
            {"name": "ITC Limited", "role": "food_beverage", "evidence_span": "ITC Limited, our Munching Partner"}]))
        result = self.run_post(post(caption), model)
        self.assertEqual(result["extracted"]["sponsor_mentions"], [])
        self.assertEqual(result["evidence"], [])
        self.assertIn("exact substring", result["extracted"]["dropped"][0]["reason"])

    def test_logo_only_post_makes_no_claim(self):
        model = FakeModel(model_output())
        result = self.run_post(post("✨🎉 #HackIndia #Sponsors @itclimited"), model)
        self.assertEqual(result["extracted"]["extraction_status"], "logo_only")
        self.assertEqual(model.calls, 0)
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["leads"], [])

    def test_two_spellings_resolve_to_one_company(self):
        caption = "Partners: Trainocate Networks India Pvt Ltd. Workshop by Trainocate Networks India."
        model = FakeModel(model_output(sponsor_mentions=[
            {"name": "Trainocate Networks India Pvt Ltd", "role": "associate",
             "evidence_span": "Partners: Trainocate Networks India Pvt Ltd"},
            {"name": "Trainocate Networks India", "role": "tech_partner",
             "evidence_span": "Workshop by Trainocate Networks India"}]))
        # The API only knows the normalised spelling; both names reach it.
        client = FakeIdentify({"trainocate networks india": [company(202, "Trainocate", 0.9)]})
        result = self.run_post(post(caption), model, client)

        ids = {m["crustdata_company_id"] for m in result["matches"]}
        self.assertEqual(ids, {202})
        self.assertEqual(match.by_company(result["matches"]),
                         {202: ["Trainocate Networks India Pvt Ltd", "Trainocate Networks India"]})
        self.assertEqual(len(client.bodies), 1)   # one batched identify call

    def test_ambiguous_match_is_a_lead_without_id(self):
        caption = "Powered by Nova Labs."
        model = FakeModel(model_output(sponsor_mentions=[
            {"name": "Nova Labs", "role": "powered_by", "evidence_span": "Powered by Nova Labs"}]))
        client = FakeIdentify({"Nova Labs": [company(1, "Nova Labs Inc", 0.91), company(2, "Nova Labs India", 0.89)]})
        result = self.run_post(post(caption), model, client)

        self.assertEqual(result["matches"][0]["match_status"], "ambiguous")
        self.assertIsNone(result["matches"][0]["crustdata_company_id"])
        self.assertEqual(result["evidence"], [])
        self.assertEqual([(l.kind, l.match_status) for l in result["leads"]], [("sponsor", "ambiguous")])

    def test_low_score_is_ambiguous(self):
        decision = match.decide([company(1, "X", 0.5)], 0.8, 0.05)
        self.assertEqual((decision["match_status"], decision["crustdata_company_id"]), ("ambiguous", None))

    def test_sponsor_ask_is_a_lead_not_a_claim(self):
        model = FakeModel(model_output(sponsor_ask=True, contact_hint="partnerships team"))
        result = self.run_post(post("We're looking for sponsors for our hackathon! DM us."), model)
        self.assertEqual(result["evidence"], [])
        self.assertEqual([l.kind for l in result["leads"]], ["sponsor_ask"])

    def test_invalid_json_retries_once_then_fails(self):
        model = FakeModel("not json", "still not json")
        result = self.run_post(post("Thanks ITC Limited!"), model)
        self.assertEqual(model.calls, 2)
        self.assertEqual(result["extracted"]["extraction_status"], "failed")

    def test_cached_extraction_needs_no_model_call(self):
        model = FakeModel(model_output())
        p = post("Thanks to everyone who came!")
        self.run_post(p, model)
        self.run_post(p, model)
        self.assertEqual(model.calls, 1)
        statuses = [r["status"] for r in self.db.execute("SELECT status FROM ledger WHERE provider = 'anthropic'")]
        self.assertEqual(statuses, ["ok", "cached"])

    def test_personal_email_in_contact_hint_is_dropped(self):
        model = FakeModel(model_output(contact_hint="priya.k@gmail.com"))
        result = self.run_post(post("Want to partner? Mail priya.k@gmail.com"), model)
        self.assertIsNone(result["extracted"]["contact_hint"])

    def test_no_taken_at_means_no_claim(self):
        caption = "Thank you ITC Limited, our title sponsor."
        model = FakeModel(model_output(sponsor_mentions=[
            {"name": "ITC Limited", "role": "title", "evidence_span": "ITC Limited, our title sponsor"}]))
        client = FakeIdentify({"ITC Limited": [company(101, "ITC Limited", 0.95)]})
        result = self.run_post(post(caption, taken_at=None), model, client)
        self.assertEqual(result["evidence"], [])
        self.assertEqual(len(result["leads"]), 1)

    def test_normalise_strips_suffixes_into_aliases(self):
        name, aliases = match.normalise("Acme Technologies Private Limited")
        self.assertEqual(name, "acme")
        self.assertEqual(aliases[0], "Acme Technologies Private Limited")
        self.assertIn("acme technologies", aliases)

    def test_restart_writes_records_once(self):
        caption = "Thank you ITC Limited, our title sponsor."
        model = FakeModel(model_output(sponsor_mentions=[
            {"name": "ITC Limited", "role": "title", "evidence_span": "ITC Limited, our title sponsor"}]))
        client = FakeIdentify({"ITC Limited": [company(101, "ITC Limited", 0.95)]})
        self.run_post(post(caption), model, client)
        self.run_post(post(caption), model, client)
        count = self.db.execute("SELECT COUNT(*) FROM ig_sponsor_records WHERE kind = 'evidence'").fetchone()[0]
        self.assertEqual(count, 1)


class MatchRulesFromLiveDataTest(unittest.TestCase):
    """Cases seen live on 22 Nov with the SIDTM post."""

    @staticmethod
    def record(company_id, name, score, domain=None):
        return {"confidence_score": score, "company_data": {
            "crustdata_company_id": company_id, "basic_info": {"name": name, "primary_domain": domain}}}

    def test_overlapping_words_are_not_a_match_with_the_live_threshold(self):
        live = ig.load_config()["match"]
        decision = match.decide([self.record(74833427, "Times of Maharashtra News", 0.8, "timesofmaharashtra.co.in")],
                                live["min_confidence"], live["ambiguity_margin"])
        self.assertEqual(decision["match_status"], "ambiguous")

    def test_same_name_twice_takes_the_record_with_a_website(self):
        decision = match.decide([self.record(2289, "ITC Limited", 1.0, "itcportal.com"),
                                 self.record(8870643, "ITC Limited", 1.0),
                                 self.record(2291, "ITC Hotels Limited", 0.8, "itchotels.com")], 1.0, 0.05)
        self.assertEqual((decision["match_status"], decision["crustdata_company_id"]), ("matched", 2289))

    def test_same_name_with_several_websites_stays_ambiguous(self):
        decision = match.decide([self.record(1, "Klaw", 1.0, "kafkawize.io"), self.record(2, "KLAW", 1.0, "klaw.ge")],
                                1.0, 0.05)
        self.assertEqual(decision["match_status"], "ambiguous")

    def test_different_names_are_never_split_by_website(self):
        decision = match.decide([self.record(1, "Nova Labs Inc", 1.0, "nova.io"), self.record(2, "Nova Labs India", 1.0)],
                                1.0, 0.05)
        self.assertEqual(decision["match_status"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
