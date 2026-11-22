"""Block 5 back-test, offline: hand-made facts in the shape block 3 saves them."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import backtest  # noqa: E402
import evidence  # noqa: E402
from records import Evidence  # noqa: E402
from tests.test_scoring import CONFIG as SCORING_CONFIG  # noqa: E402

CANDIDATES_CFG = {"signals_full_at": {"past_sponsorships": 2, "active_hiring": 3, "city_presence": 1}}
CFG = {"min_sponsors": 2, "top_k": 2, "min_events": 5, "min_decided_share": 0.2}


def sponsor(company_id, event_name, posted, event_dates=(), organizer="organizer", name=None,
            status="verified", url=None):
    """A past-sponsor fact as block 3 saves it under company:<id>."""
    return company_id, Evidence(
        claim=f"{name or company_id} sponsored {event_name}", source_type="linkedin_post",
        value={"sponsor_name_as_written": name or f"Sponsor {company_id}", "role": "associate",
               "crustdata_company_id": company_id, "event_name": event_name,
               "event_dates": list(event_dates), "organizer_handle": organizer},
        source_url=url or f"https://www.linkedin.com/posts/{organizer}-{event_name}-{company_id}",
        quote="q", record_date=posted, retrieved_at="2026-11-22T09:00:00+00:00", status=status)


def job(company_id, posted="2026-11-20T10:00:00"):
    return company_id, Evidence(claim=f"Company {company_id} posted a job", value="Brand Manager",
                                source_url=f"https://www.linkedin.com/jobs/view/{company_id}", quote="q",
                                record_date=posted, retrieved_at="2026-11-22T09:00:00+00:00",
                                source_type="crustdata:/job/search")


def run(facts):
    return backtest.run(facts, "conference", CFG, CANDIDATES_CFG, SCORING_CONFIG)


class EventTest(unittest.TestCase):
    def test_name_key_drops_ordinals_and_punctuation(self):
        self.assertEqual(backtest.event_name_key("14th BMR International Conference, 2026"),
                         "bmr international conference 2026")

    def test_variant_names_and_shared_dates_are_one_event(self):
        facts = [sponsor(1, "BRICS Conference 2026", "2026-09-10T08:00:00Z", organizer="ficci"),
                 sponsor(2, "BRICS", "2026-09-12T08:00:00Z", organizer="lancesoft"),   # sponsor's own post
                 sponsor(3, "Delhi Fintech Meet", "2026-09-11T08:00:00Z", event_dates=["2026-10-01"]),
                 sponsor(4, None, "2026-09-11T08:00:00Z", event_dates=["2026-10-01"])]
        events = backtest.past_events(facts)
        self.assertEqual([sorted(e.sponsors) for e in events], [[1, 2], [3, 4]])
        self.assertEqual(events[0].cutoff, "2026-09-10")

    def test_fact_with_no_event_name_or_date_is_not_used(self):
        facts = [sponsor(1, None, "2026-09-10T08:00:00Z"), sponsor(2, "Alpha Summit", "2026-09-10T08:00:00Z")]
        self.assertEqual([sorted(e.sponsors) for e in backtest.past_events(facts)], [[2]])


class LeakageTest(unittest.TestCase):
    def setUp(self):
        self.event_facts = [sponsor(1, "BRICS Conference 2026", "2026-09-10T08:00:00Z"),
                            sponsor(2, "BRICS Conference 2026", "2026-09-10T08:00:00Z")]
        self.event = backtest.past_events(self.event_facts)[0]

    def test_earlier_post_about_the_same_event_under_another_name_is_not_history(self):
        teaser = sponsor(1, "BRICS", "2026-08-20T08:00:00Z", organizer="lancesoft")
        self.assertEqual(backtest.history_hits(self.event, self.event_facts + [teaser]), [])

    def test_fact_on_or_after_the_cutoff_never_counts(self):
        later = [sponsor(1, "Alpha Summit 2026", "2026-09-10T00:00:00Z"),
                 sponsor(1, "Beta Summit 2026", "2026-10-01T08:00:00Z")]
        self.assertEqual(backtest.history_hits(self.event, self.event_facts + later), [])

    def test_unverified_fact_never_counts(self):
        old = sponsor(1, "Alpha Summit 2025", "2025-09-01T08:00:00Z", status="cannot_verify")
        self.assertEqual(backtest.history_hits(self.event, self.event_facts + [old]), [])

    def test_earlier_sponsorship_of_another_event_counts(self):
        old = sponsor(1, "Alpha Summit 2025", "2025-09-01T08:00:00Z")
        hits = backtest.history_hits(self.event, self.event_facts + [old])
        self.assertEqual([(h.company_id, h.signal) for h in hits], [(1, "past_sponsorships")])

    def test_sponsor_who_wrote_the_post_is_still_a_candidate(self):
        facts = self.event_facts + [sponsor(3, "BRICS Conference 2026", "2026-09-11T08:00:00Z",
                                            organizer="Sponsor 3")]
        event = backtest.past_events(facts)[0]
        ranked = backtest.rank_as_of(event, facts, {1: "Sponsor 1", 2: "Sponsor 2", 3: "Sponsor 3"},
                                     "conference", CANDIDATES_CFG, SCORING_CONFIG)
        self.assertEqual(sorted(c.company_id for c in ranked), [1, 2, 3])


class MeasureTest(unittest.TestCase):
    def test_ties_count_half_and_unknown_is_lowest(self):
        from records import Candidate
        ranked = [Candidate(1, "a", None, score=0.5), Candidate(2, "b", None, score=0.5),
                  Candidate(3, "c", None, score=None), Candidate(4, "d", None, score=None)]
        m = backtest.measure(ranked, sponsors={1, 3}, top_k=1)
        # pairs: 1>4 win, 1=2 tie, 3<2 loss, 3=4 tie -> (1 + .5 + 0 + .5) / 4
        self.assertEqual(m["auc"], 0.5)
        self.assertEqual(m["decided"], 2)
        self.assertEqual(m["sponsors_known"], 1)
        self.assertEqual(m["recall_at_k"], 0.5)
        self.assertIsNone(backtest.measure(ranked, sponsors={1, 3}, top_k=4)["recall_at_k"])


class RunTest(unittest.TestCase):
    def repeat_sponsors(self):
        """Companies 1 and 2 sponsor an event every month; 3 and 4 only post jobs."""
        names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]
        facts = [sponsor(c, f"{n} Summit 2026", f"2026-0{i + 1}-15T08:00:00Z")
                 for i, n in enumerate(names) for c in (1, 2)]
        return facts + [job(3), job(4)]

    def test_repeat_sponsors_rank_above_companies_with_no_history(self):
        result = run(self.repeat_sponsors())
        t = result["total"]
        self.assertEqual((t["events_found"], t["events_tested"], t["sponsors"]), (6, 6, 12))
        # The first event has no history: its 4 pairs tie. The other 20 pairs are won.
        self.assertAlmostEqual(t["auc"], 22 / 24)
        self.assertAlmostEqual(t["decided_share"], 20 / 24)
        self.assertEqual(t["status"], "verified")
        self.assertIn("ranked above a non-sponsor 92%", backtest.report(result))

    def test_too_few_events_cannot_verify(self):
        result = run(self.repeat_sponsors()[:4] + [job(3)])
        self.assertEqual(result["total"]["status"], "cannot_verify")
        self.assertIn("only 2 testable events", result["total"]["missing"])
        self.assertTrue(backtest.report(result).splitlines()[2].startswith("**Cannot verify.**"))

    def test_one_off_sponsors_rank_below_earlier_sponsors_so_the_test_can_fail(self):
        # Each event has new sponsors; earlier events' sponsors are its non-sponsors with history.
        facts = [sponsor(10 * i + c, f"Event {i} 2026", f"2026-0{i + 1}-15T08:00:00Z")
                 for i in range(6) for c in (1, 2)]
        t = run(facts)["total"]
        self.assertEqual(t["status"], "verified")
        self.assertLess(t["auc"], 0.5)

    def test_mostly_ties_cannot_verify(self):
        # Six events, all posted the same day: nobody has history before any of them.
        facts = [sponsor(10 * i + c, f"Event {i} 2026", "2026-09-15T08:00:00Z", event_dates=[f"2026-1{i % 3}-0{i + 1}"])
                 for i in range(6) for c in (1, 2)] + [job(99)]
        t = run(facts)["total"]
        self.assertEqual(t["decided_share"], 0)
        self.assertEqual(t["status"], "cannot_verify")
        self.assertIn("pairs were decided", t["missing"])


class LoadTest(unittest.TestCase):
    def test_loads_company_facts_once_across_runs(self):
        db = agent_db.connect(":memory:")
        cid, fact = sponsor(7, "Alpha Summit 2026", "2026-03-01T08:00:00Z")
        for run_id in ("run-a", "run-b"):
            evidence.save(db, run_id, f"company:{cid}", fact)
        evidence.save(db, "run-a", "host", fact)
        loaded = backtest.load_facts(db, ["run-a", "run-b"])
        db.close()
        self.assertEqual([(c, f.value["event_name"]) for c, f in loaded], [(7, "Alpha Summit 2026")])

    def test_real_config_has_the_keys(self):
        self.assertEqual(set(backtest.load_config()),
                         {"min_sponsors", "top_k", "min_events", "min_decided_share"})


if __name__ == "__main__":
    unittest.main()
