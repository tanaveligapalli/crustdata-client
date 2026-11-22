"""People lists and replies on the web screen, offline: fake person search, demo inbox, outbox."""

import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import agent  # noqa: E402
import agent_db  # noqa: E402
import outreach  # noqa: E402
import server  # noqa: E402
from test_contact_choice import CFG, Client  # noqa: E402
from test_outreach import BRIEF, HOST_OK, Sender, candidate  # noqa: E402
from test_web import RUN, SECRET_KEYS, Base, keys_in  # noqa: E402

COMPANY = candidate(111111, "Example Corp")
OTHER = candidate(222222, "Globex")


class Replies(unittest.TestCase):
    """Block 6 in hold mode: nothing reaches a sponsor until a person approves it on the screen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = agent_db.connect(Path(self.tmp.name) / "a.sqlite3")
        self.job = agent.Agent(RUN, False, self.db, Client(), {"outreach": CFG}, False)
        self.outbox = mock.patch.object(agent, "OUTBOX", Path(self.tmp.name) / "outbox")
        self.outbox.start()
        for c in (COMPANY, OTHER):     # both pitched and sent
            outreach.start_campaign(self.db, RUN, BRIEF, [(c, None)], CFG, agent._sender(self.job),
                                    host_check=HOST_OK, ask=lambda _: "y", show=lambda _: None)

    def tearDown(self):
        self.outbox.stop()
        self.db.close()
        self.tmp.cleanup()

    def sent(self):
        return sorted((Path(self.tmp.name) / "outbox" / RUN).glob("*.eml"))

    def reply(self, company, text, sender="priya@sponsor.example"):
        agent.add_demo_reply(self.job, company.company_id, sender, text)
        agent.check_replies(self.job, BRIEF)

    def state(self, company):
        return outreach.get_row(self.db, RUN, company.company_id)["state"]

    def test_interested_reply_waits_for_approval_then_books_the_meeting(self):
        before = len(self.sent())
        self.reply(COMPANY, "Sounds good, let's set up a call next week.")
        self.assertEqual(self.state(COMPANY), "replied_interested")
        held = agent.held_replies(self.job)
        self.assertEqual([(h["kind"], h["done_state"]) for h in held], [("invite", "meeting_booked")])
        self.assertEqual(len(self.sent()), before)                    # nothing sent yet

        self.assertEqual(agent.send_held_replies(self.job, [held[0]["id"]]), 1)
        self.assertEqual(self.state(COMPANY), "meeting_booked")
        self.assertEqual(len(self.sent()), before + 1)
        self.assertEqual(agent.held_replies(self.job), [])
        self.assertEqual(agent.send_held_replies(self.job, [held[0]["id"]]), 0)   # never twice
        self.assertEqual(len(self.sent()), before + 1)

    def test_the_approved_text_is_the_text_sent(self):
        self.reply(COMPANY, "Interested! Let's talk.")
        held = agent.held_replies(self.job)[0]
        agent.send_held_replies(self.job, [held["id"]])
        sent = self.sent()[-1].read_text(encoding="utf-8", errors="replace")
        self.assertIn(held["draft"].body.splitlines()[0], sent)

    def test_a_no_is_rejected_without_any_email(self):
        before = len(self.sent())
        self.reply(COMPANY, "Thanks, but we're not interested this year.")
        self.assertEqual(self.state(COMPANY), "declined")
        self.assertEqual(agent.held_replies(self.job), [])
        self.assertEqual(len(self.sent()), before)

    def test_opt_out_is_rejected_and_remembered(self):
        self.reply(COMPANY, "Please unsubscribe us.")
        self.assertEqual(self.state(COMPANY), "opted_out")
        self.assertTrue(outreach.is_opted_out(self.db, COMPANY.company_id))

    def test_two_invites_waiting_for_approval_get_different_slots(self):
        self.reply(COMPANY, "Let's set up a call.")
        self.reply(OTHER, "Interested, let's meet.")
        bodies = [h["draft"].body for h in agent.held_replies(self.job)]
        self.assertEqual(len(bodies), 2)
        self.assertNotEqual(bodies[0], bodies[1].replace("Globex", "Example Corp"))
        self.assertIn("24 Nov 2026, 15:00", bodies[0])
        self.assertIn("24 Nov 2026, 16:00", bodies[1])

    def test_not_sending_a_draft_hands_the_thread_to_a_person(self):
        self.reply(COMPANY, "Let's set up a call.")
        held = agent.held_replies(self.job)[0]
        before = len(self.sent())
        agent.discard_held_replies(self.job, [held["id"]])
        self.assertEqual(self.state(COMPANY), "needs_person")
        self.assertEqual(len(self.sent()), before)

    def test_each_reply_is_handled_once(self):
        self.reply(COMPANY, "Let's set up a call.")
        agent.check_replies(self.job, BRIEF)                         # read the inbox again
        self.assertEqual(len(agent.held_replies(self.job)), 1)

    def test_only_companies_we_emailed_can_reply(self):
        third = candidate(333333, "Initech")
        outreach.start_campaign(self.db, RUN, BRIEF, [(third, None)], CFG, Sender(),
                                host_check=HOST_OK, ask=lambda _: "n", show=lambda _: None)   # drafted only
        with self.assertRaises(ValueError):
            agent.add_demo_reply(self.job, third.company_id, "a@b.example", "Hi")
        with self.assertRaises(ValueError):
            agent.add_demo_reply(self.job, COMPANY.company_id, "not-an-address", "Hi")

    def test_reply_log_shows_sender_label_and_text(self):
        self.reply(COMPANY, "Not interested, thanks.", sender="rahul@example.org")
        log = agent.reply_log(self.db, RUN)
        self.assertEqual([(r["company"], r["label"], r["sender"], r["text"]) for r in log],
                         [("Example Corp", "declined", "rahul@example.org", "Not interested, thanks.")])


class Screen(Base):
    """What the page receives, and which actions are allowed."""

    def add_people(self, company_id, names):
        people = [{"company_id": company_id, "name": n, "title": "Brand Manager", "profile_url": "https://x.test/in/a",
                   "business_email": None,
                   "evidence": [{"claim": "c", "value": "Brand Manager", "source_url": "crustdata:/person/search",
                                 "quote": "q", "record_date": "2024-01-01", "retrieved_at": "2026-11-22",
                                 "source_type": "crustdata:/person/search", "status": "verified", "missing": None}]}
                  for n in names]
        db = agent_db.connect(self.path)
        db.execute("INSERT INTO steps VALUES (?, ?, ?, ?)", (RUN, f"contacts:{company_id}", json.dumps(people), "t"))
        db.commit()
        db.close()

    def view(self, app=None):
        status, view = (app or self.app()).handle("GET", f"/api/runs/{RUN}", {}, {})
        self.assertEqual(status, 200)
        return view

    def test_people_listed_for_each_picked_company(self):
        self.add_people(7, ["Asha Rao", "Vikram Sethi"])
        view = self.view()
        self.assertEqual([(g["company"], [p["name"] for p in g["people"]]) for g in view["people"]],
                         [("Acme", ["Asha Rao", "Vikram Sethi"])])
        self.assertEqual(view["people"][0]["people"][0],
                         {"name": "Asha Rao", "title": "Brand Manager", "since": "2024-01-01",
                          "profile_url": "https://x.test/in/a"})
        self.assertFalse(keys_in(view) & SECRET_KEYS)

    def test_people_disappear_once_a_pitch_is_drafted(self):
        self.add_people(7, ["Asha Rao"])
        db = agent_db.connect(self.path)
        outreach.ensure_tables(db)
        db.execute("INSERT INTO outreach (run_id, company_id, company_name, state, contact_json, updated_at) "
                   "VALUES (?, 7, 'Acme', 'drafted', NULL, 't')", (RUN,))
        db.commit()
        db.close()
        self.assertEqual(self.view()["people"], [])

    def test_pitches_pass_the_hosts_choice_to_the_worker(self):
        calls = []
        self.workers.pitches = lambda description, ids, say, choices=None: calls.append((ids, choices))
        status, _ = self.app().handle("POST", f"/api/runs/{RUN}/pitches", {},
                                      {"company_ids": [7], "choices": {"7": 1}})
        self.assertEqual(status, 202)
        for _ in range(50):
            if calls:
                break
            import time
            time.sleep(0.02)
        self.assertEqual(calls, [([7], {7: 1})])

    def test_replay_mode_never_reads_or_sends_replies(self):
        app = self.app(offline=True)
        for kind in ("contacts", "replies", "send-replies", "demo-reply"):
            status, _ = app.handle("POST", f"/api/runs/{RUN}/{kind}", {}, {"company_ids": [7], "draft_ids": [1]})
            self.assertEqual(status, 409, kind)

    def test_demo_inbox_is_off_when_gmail_is_set_up(self):
        with mock.patch.object(server, "email_destination", return_value="team_inbox"):
            status, body = self.app().handle("POST", f"/api/runs/{RUN}/demo-reply", {},
                                             {"company_id": 7, "sender": "a@b.example", "text": "hi"})
        self.assertEqual(status, 409)
        self.assertIn("Gmail is set up", body["error"])

    def test_sending_replies_needs_an_approved_draft(self):
        status, _ = self.app().handle("POST", f"/api/runs/{RUN}/send-replies", {}, {})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
