"""Block 6 outreach, offline: SMTP, IMAP and the model are fakes."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import outreach  # noqa: E402
from records import Brief, Candidate, Contact, Evidence, Signal, Tier  # noqa: E402

RUN = "run-test"
CONFIG = {
    "sender": "agent@team.test", "team_inbox": "inbox@team.test", "host_inbox": None,
    "require_verified_host": True, "opt_out_line": "Reply \"unsubscribe\" to stop.",
    "contact_roles": ["Head of Partnerships", "Brand Manager"],
    "follow_up_after_days": 3, "max_follow_ups": 2, "meeting_minutes": 30,
    "meeting_slots": ["2026-11-24T15:00:00+05:30", "2026-11-24T16:00:00+05:30"], "booking_link": None,
}
BRIEF = Brief(event_name="TechNova Summit 2027", profile="conference", event_date="2027-03-14",
              city="Bengaluru", audience_size=2000, audience="startup founders and engineers",
              host_name="TechNova Foundation", host_website="technova.org",
              tiers=[Tier("Title sponsor", 500000, ["main stage branding"]), Tier("Gold", 200000, [])])
HOST_OK = Evidence("TechNova Foundation is a real organization", "technova.org", "https://technova.org",
                   "basic_info.primary_domain = technova.org", "2026-11-01", "2026-11-22T10:00:00+00:00",
                   "crustdata:/company/identify")


def candidate(cid, name):
    ev = Evidence(f"{name} sponsored DevFest Pune 2026", "sponsor", "https://example.org/devfest",
                  f"{name} - Gold Partner", "2026-09-04", "2026-11-22T10:00:00+00:00", "web_fetch")
    return Candidate(cid, name, f"{name.lower()}.com", signals=[Signal("past_sponsorships", 1.0, [ev])])


def contact(cid, name):
    return Contact(cid, name, "Head of Partnerships", f"https://linkedin.com/in/{cid}", None)


SELECTED = [(candidate(1, "Acme"), contact(1, "Priya Sharma")),
            (candidate(2, "Globex"), contact(2, "Arjun Rao")),
            (candidate(3, "Initech"), None)]


class Sender:
    def __init__(self, fail_on=None):
        self.sent, self.fail_on = [], fail_on

    def __call__(self, msg):
        if self.fail_on and self.fail_on in msg["Subject"]:
            raise ConnectionError("SMTP died mid-send")
        self.sent.append(msg)

    def subjects(self):
        return [m["Subject"] for m in self.sent]


def reply(mid, cid, body, sender="sponsor@acme.test"):
    return outreach.Reply(mid, sender, f"Re: [to: x] [ref:{RUN}:{cid}] Sponsoring", body)


def yes(_):
    return "y"


class Base(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")
        self.quiet = lambda _: None

    def tearDown(self):
        self.db.close()

    def campaign(self, sender, ask=yes, model=None, selected=SELECTED):
        return outreach.start_campaign(self.db, RUN, BRIEF, selected, CONFIG, sender, model=model,
                                       host_check=HOST_OK, ask=ask, show=self.quiet)

    def replies(self, items, sender, model=None):
        return outreach.handle_replies(self.db, RUN, BRIEF, CONFIG, lambda: items, sender, model,
                                       ask=yes, show=self.quiet)

    def state(self, cid):
        return outreach.get_row(self.db, RUN, cid)["state"]


class CampaignTests(Base):
    def test_nothing_sends_without_y(self):
        s = Sender()
        self.campaign(s, ask=lambda _: "n")
        self.assertEqual(s.sent, [])
        self.assertEqual(self.state(1), "drafted")

    def test_every_email_goes_to_team_inbox_with_target_tag_and_opt_out(self):
        s = Sender()
        self.campaign(s)
        self.assertEqual(len(s.sent), 3)
        for msg in s.sent:
            self.assertEqual(msg["To"], "inbox@team.test")
            self.assertIn("unsubscribe", msg.get_content())
        self.assertIn("[to: Head of Partnerships, Acme]", s.subjects()[0])
        self.assertIn("[to: sponsorship team, Initech]", s.subjects()[2])

    def test_unverified_host_blocks_outreach(self):
        bad = Evidence("host", "x", "u", "q", None, "t", "web_fetch")  # no date -> cannot_verify
        with self.assertRaises(outreach.OutreachBlocked):
            outreach.start_campaign(self.db, RUN, BRIEF, SELECTED, CONFIG, Sender(), host_check=bad,
                                    ask=yes, show=self.quiet)

    def test_rerun_sends_nothing_twice(self):
        s = Sender()
        self.campaign(s)
        self.campaign(s)
        self.assertEqual(len(s.sent), 3)

    def test_crash_mid_send_then_restart(self):
        first = Sender(fail_on="Globex")              # Acme sent, Globex half-sent, Initech not reached
        with self.assertRaises(ConnectionError):
            self.campaign(first)
        self.assertEqual(len(first.sent), 1)

        second = Sender()
        self.campaign(second)
        self.assertEqual([m["Subject"] for m in second.sent if "Acme" in m["Subject"]], [])
        self.assertEqual(self.state(1), "sent")
        self.assertEqual(self.state(2), "needs_person")   # can't tell if it went: a person checks
        self.assertEqual(self.state(3), "sent")
        self.assertEqual(len(second.sent), 1)

    def test_crash_after_send_before_state_update(self):
        s = Sender()
        real = outreach.set_state

        def die_on_sent(db, run_id, cid, state, reason):
            if state == "sent" and cid == 1:
                raise KeyboardInterrupt("killed")
            return real(db, run_id, cid, state, reason)

        outreach.set_state = die_on_sent
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.campaign(s)
        finally:
            outreach.set_state = real
        asked = []
        self.campaign(s, ask=lambda q: asked.append(q) or "y")
        self.assertEqual(sum("Acme" in x for x in s.subjects()), 1)
        self.assertEqual(self.state(1), "sent")
        self.assertFalse(any("Acme" in q for q in asked))  # approved before the crash: not asked again

    def test_model_pitch_with_invented_number_falls_back_to_template(self):
        def model(system, user, schema):
            return json.dumps({"subject": "Hi", "body": "We have 50,000 attendees!"}), 0.001

        d = outreach.draft_pitch(*SELECTED[0], BRIEF, model, self.db, RUN)
        self.assertNotIn("50,000", d.body)
        self.assertIn("2,000 attendees", d.body)
        self.assertTrue(d.notes)

    def test_model_pitch_with_brief_numbers_is_kept(self):
        def model(system, user, schema):
            return json.dumps({"subject": "TechNova", "body": "2,000 founders, Title sponsor ₹500,000."}), 0.001

        d = outreach.draft_pitch(*SELECTED[0], BRIEF, model, self.db, RUN)
        self.assertEqual(d.body, "2,000 founders, Title sponsor ₹500,000.")
        cost = self.db.execute("SELECT COUNT(*) FROM ledger WHERE provider = 'anthropic'").fetchone()[0]
        self.assertEqual(cost, 1)


class ReplyTests(Base):
    def setUp(self):
        super().setUp()
        self.s = Sender()
        self.campaign(self.s)
        self.s.sent.clear()

    def test_interested_books_a_meeting_once(self):
        r = reply("<m1>", 1, "Sounds good, let's set up a call.\n\nOn Mon, Nov 23 wrote:\n> old text")
        self.replies([r], self.s)
        self.replies([r], self.s)                      # same reply read again
        self.assertEqual(self.state(1), "meeting_booked")
        self.assertEqual(len(self.s.sent), 1)
        ics = [p for p in self.s.sent[0].iter_attachments()][0].get_content()
        self.assertIn("DTSTART:20261124T093000Z", ics)

    def test_two_meetings_get_different_slots(self):
        self.replies([reply("<a>", 1, "Interested!"), reply("<b>", 2, "Interested, keen to talk")], self.s)
        slots = [list(m.iter_attachments())[0].get_content() for m in self.s.sent]
        self.assertIn("T093000Z", slots[0])
        self.assertIn("T103000Z", slots[1])

    def test_opt_out_wins_and_is_never_emailed_again(self):
        def model(system, user, schema):                # model wrongly says interested
            return json.dumps({"state": "replied_interested"}), 0.0

        self.replies([reply("<o>", 1, "Please unsubscribe me, but interested in future?")], self.s, model)
        self.assertEqual(self.state(1), "opted_out")
        self.assertEqual(self.s.sent, [])
        # a new run: still never contacted
        outreach.start_campaign(self.db, "run-2", BRIEF, SELECTED[:1], CONFIG, self.s, host_check=HOST_OK,
                                ask=yes, show=self.quiet)
        self.assertEqual(self.s.sent, [])
        self.assertEqual(outreach.get_row(self.db, "run-2", 1)["state"], "opted_out")

    def test_question_answered_only_from_brief(self):
        def model(system, user, schema):
            if "Classify" in system:
                return json.dumps({"state": "replied_question"}), 0.0
            return json.dumps({"answerable": True, "answer": "The Title sponsor tier is ₹500,000."}), 0.0

        self.replies([reply("<q>", 1, "What does the title tier cost?")], self.s, model)
        self.assertEqual(len(self.s.sent), 1)
        self.assertIn("₹500,000", self.s.sent[0].get_content())
        self.assertEqual(self.s.sent[0]["In-Reply-To"], "<q>")

    def test_answer_with_invented_number_goes_to_host(self):
        def model(system, user, schema):
            if "Classify" in system:
                return json.dumps({"state": "replied_question"}), 0.0
            return json.dumps({"answerable": True, "answer": "It costs ₹3,00,000."}), 0.0

        self.replies([reply("<q>", 1, "What does the title tier cost?")], self.s, model)
        self.assertEqual(self.state(1), "needs_person")
        self.assertIn("[handoff]", self.s.sent[0]["Subject"])
        self.assertNotIn("3,00,000", self.s.sent[0].get_content().split("Why")[1])

    def test_negotiation_is_handed_to_host(self):
        self.replies([reply("<n>", 2, "Could you do a discount on Gold?")], self.s)
        self.assertEqual(self.state(2), "negotiating")
        self.assertIn("[handoff] Globex", self.s.sent[0]["Subject"])

    def test_declined(self):
        self.replies([reply("<d>", 3, "Not interested this year, thanks.")], self.s)
        self.assertEqual(self.state(3), "declined")
        self.assertEqual(self.s.sent, [])

    def test_own_mail_and_other_runs_are_ignored(self):
        self.replies([reply("<own>", 1, "Interested", sender="Agent <agent@team.test>"),
                      outreach.Reply("<x>", "a@b.test", "Re: [ref:other-run:1] hi", "Interested")], self.s)
        self.assertEqual(self.state(1), "sent")


class SendOnceAcrossRunsTests(Base):
    def test_new_run_id_cannot_resend_the_same_event_to_the_same_company(self):
        s = Sender()
        self.campaign(s)
        sent = len(s.sent)
        outreach.start_campaign(self.db, "run-second", BRIEF, SELECTED, CONFIG, s,
                                host_check=HOST_OK, ask=yes, show=self.quiet)
        self.assertEqual(len(s.sent), sent)

    def test_a_different_event_can_pitch_the_same_company(self):
        s = Sender()
        self.campaign(s)
        other = Brief(**{**BRIEF.__dict__, "event_name": "TechNova Summit 2028", "event_date": "2028-03-14"})
        outreach.start_campaign(self.db, "run-other", other, SELECTED[:1], CONFIG, s,
                                host_check=HOST_OK, ask=yes, show=self.quiet)
        self.assertEqual(len(s.sent), 4)


class FindContactTests(unittest.TestCase):
    class Client:
        def __init__(self, drop=()):
            self.bodies, self.drop = [], drop

        def call(self, path, body):
            from crustdata_client import ApiResponse
            self.bodies.append((path, body))
            fixture = json.loads((ROOT / "tests" / "fixtures" / "person_search.json").read_text())
            fixture["profiles"] = [p for p in fixture["profiles"] if p["basic_profile"]["name"] not in self.drop]
            return ApiResponse(fixture, "2026-11-22T10:00:00+00:00", False, 0.69)

    ROLES = {**CONFIG, "contact_roles": ["Head of Partnerships", "Partnerships", "Marketing Manager"],
             "contact_exclude_titles": ["private equity"]}

    def test_picks_best_role_with_sourced_evidence(self):
        client = self.Client()
        found = outreach.find_contact(client, candidate(111111, "Example Corp"), self.ROLES)
        self.assertEqual((found.name, found.title), ("Vikram Sethi", "Head of Partnerships"))
        self.assertIsNone(found.business_email)
        self.assertEqual(found.evidence[0].source_type, "crustdata:/person/search")
        self.assertEqual(found.evidence[0].record_date, "2023-01-01")
        path, body = client.bodies[0]
        self.assertEqual(path, "/person/search")
        self.assertEqual(body["filters"]["conditions"][0]["value"], 111111)

    def test_unrelated_partnerships_title_is_never_picked(self):
        # Only "Private Equity Partnerships" matches "Partnerships" once Vikram is gone: no contact at all.
        roles = {**self.ROLES, "contact_roles": ["Partnerships"]}
        client = self.Client(drop=("Vikram Sethi",))
        self.assertIsNone(outreach.find_contact(client, candidate(111111, "Example Corp"), roles))
        without_rule = {**roles, "contact_exclude_titles": []}
        found = outreach.find_contact(client, candidate(111111, "Example Corp"), without_rule)
        self.assertEqual(found.name, "Neha Iyer")        # proves the exclusion rule is what stopped it


class FollowUpTests(Base):
    def test_two_follow_ups_then_no_response(self):
        s = Sender()
        self.campaign(s, selected=SELECTED[:1])
        now = datetime.now(timezone.utc)
        for days in (4, 8, 12, 16):
            outreach.run_follow_ups(self.db, RUN, BRIEF, CONFIG, s, now=now + timedelta(days=days),
                                    ask=yes, show=self.quiet)
        self.assertEqual(len(s.sent), 3)                # intro + 2 follow-ups
        self.assertEqual(self.state(1), "no_response")

    def test_no_follow_up_before_the_wait(self):
        s = Sender()
        self.campaign(s, selected=SELECTED[:1])
        outreach.run_follow_ups(self.db, RUN, BRIEF, CONFIG, s, ask=yes, show=self.quiet)
        self.assertEqual(len(s.sent), 1)


class HelperTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(outreach.numbers_in("₹5,00,000 on 14 March 2027"), {"500000", "14", "2027"})
        self.assertEqual(outreach.unsupported_numbers("₹500,000", ["₹5,00,000"]), set())

    def test_new_text_drops_quotes(self):
        self.assertEqual(outreach.new_text("Yes!\n> quoted\nOn Tue, X wrote:\nold"), "Yes!")

    def test_full_pipeline_to_meeting_booked(self):
        db = agent_db.connect(":memory:")
        s = Sender()
        q = lambda _: None
        outreach.start_campaign(db, RUN, BRIEF, SELECTED[:1], CONFIG, s, host_check=HOST_OK, ask=yes, show=q)
        outreach.handle_replies(db, RUN, BRIEF, CONFIG, lambda: [reply("<i>", 1, "Interested, let's meet")],
                                s, None, ask=yes, show=q)
        rows = outreach.pipeline(db, RUN)
        self.assertEqual(rows[0]["state"], "meeting_booked")
        self.assertIn("meeting invite sent", rows[0]["why"])
        db.close()


class ApprovalSurvivesCrashTest(Base):
    def test_after_a_crash_the_approved_text_is_sent_not_a_redraft(self):
        wording = {"now": "the approved wording"}

        def model(system, user, schema):
            return json.dumps({"subject": "Sponsoring TechNova", "body": f"Hello, this is {wording['now']}."}), 0.0

        real_send_once = outreach.send_once

        def die_before_sending(*args, **kwargs):
            raise KeyboardInterrupt("killed after approval, before sending")

        outreach.send_once = die_before_sending
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.campaign(Sender(), model=model)
        finally:
            outreach.send_once = real_send_once
        self.assertEqual(self.state(1), "approved")

        wording["now"] = "a different redraft"
        s, asked = Sender(), []
        self.campaign(s, model=model, ask=lambda q: asked.append(q) or "y")
        acme = [m for m in s.sent if "Acme" in m["Subject"]]
        self.assertEqual(len(acme), 1)
        self.assertIn("the approved wording", acme[0].get_content())
        self.assertFalse(any("Acme" in q for q in asked))   # already approved: not asked again


if __name__ == "__main__":
    unittest.main()
