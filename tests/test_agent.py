"""The end-to-end stages, offline: replay from saved steps, the outbox, stable run IDs."""

import sys
import tempfile
import unittest
from dataclasses import asdict
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import agent_db  # noqa: E402
from records import Brief, Candidate, Evidence, Signal  # noqa: E402
from steps import run_step  # noqa: E402

BRIEF = Brief("TechNova Summit 2027", "conference", "2027-03-14", "Pune", 500, "developers",
              "TechNova Foundation", "technova.org")
FACT = Evidence("Acme posted a 'Head of Partnerships' job", "Head of Partnerships", "https://x.in/job/1",
                "job_details.title = Head of Partnerships", "2026-11-20", "2026-11-22T09:00:00+00:00",
                "crustdata:/job/search")


class NoCalls:
    """A Crustdata client that fails the test if anything reaches it."""

    def call(self, path, body):
        raise AssertionError(f"unexpected API call to {path}")

    request = call


def offline_agent(db):
    config = {"scoring": {"profiles": {"conference": {"active_hiring": 1.0}}}, "outreach": {},
              "brief": {"profile_keywords": {"conference": ["summit"]}}}
    return agent.Agent("run-test", True, db, NoCalls(), config, email_ready=False)


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_the_same_description_is_the_same_run(self):
        self.assertEqual(agent.run_id_for("Summit in Pune"), agent.run_id_for("  Summit in Pune\n"))
        self.assertEqual(agent.run_id_for("Summit\r\nin Pune"), agent.run_id_for("Summit\nin Pune"))
        self.assertNotEqual(agent.run_id_for("Summit in Pune"), agent.run_id_for("Summit in Goa"))

    def test_finished_stages_replay_without_any_call(self):
        job = offline_agent(self.db)
        acme = Candidate(7, "Acme", "acme.in", ["hiring"], signals=[Signal("active_hiring", 1.0, [FACT])])
        run_step(self.db, job.run_id, "brief", lambda: {"brief": asdict(BRIEF), "dropped": []})
        run_step(self.db, job.run_id, "host_check", lambda: asdict(FACT))
        run_step(self.db, job.run_id, "candidates", lambda: {"candidates": [asdict(acme)], "failed_pools": []})

        brief, _ = agent.make_brief(job, "any text")
        ranked, failed = agent.find_candidates(job, brief)

        self.assertEqual(brief, BRIEF)
        self.assertEqual(agent.check_host(job, brief), FACT)
        self.assertEqual([(c.name, c.score) for c in ranked], [("Acme", 1.0)])
        self.assertEqual(failed, [])

    def test_offline_never_calls_the_model(self):
        with self.assertRaises(agent.NotCached):
            agent.make_brief(offline_agent(self.db), "a description that was never run online")

    def test_without_gmail_emails_go_to_the_outbox(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(agent, "OUTBOX", Path(folder)):
            send = agent.outbox_sender("run-test")
            for subject in ("one", "two"):
                message = EmailMessage()
                message["Subject"] = subject
                send(message)
            self.assertEqual(sorted(p.name for p in (Path(folder) / "run-test").iterdir()), ["001.eml", "002.eml"])

    def test_cost_adds_credits_and_model_dollars(self):
        import ledger

        ledger.record(self.db, "run-test", "crustdata", "/job/search", "ok", credits=3)
        ledger.record(self.db, "run-test", "anthropic", "messages", "ok", cost_usd=0.02)
        self.assertEqual(agent.cost(offline_agent(self.db)), {"credits": 3.0, "model_usd": 0.02})


if __name__ == "__main__":
    unittest.main()
