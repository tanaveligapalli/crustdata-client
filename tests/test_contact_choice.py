"""The host picks which person to pitch at each company, and block 6 uses that person. Offline."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import agent_db  # noqa: E402
import outreach  # noqa: E402
from crustdata_client import ApiResponse  # noqa: E402
from test_outreach import BRIEF, CONFIG, HOST_OK, RUN, Sender, candidate  # noqa: E402

CFG = {**CONFIG, "contact_roles": ["Head of Partnerships", "Brand Manager", "Marketing Manager"],
       "contact_exclude_titles": ["intern"]}
COMPANY = candidate(111111, "Example Corp")


def person(name, title, company_id=111111, end_date=None):
    return {"basic_profile": {"name": name, "current_title": title},
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.test/in/{name[0]}"}},
            "experience": {"employment_details": {"current": [
                {"title": title, "crustdata_company_id": company_id, "start_date": "2024-01-01T00:00:00",
                 "end_date": end_date}]}}}


PEOPLE = [person("Meera Nair", "Marketing Manager"),
          person("Left Person", "Head of Partnerships", end_date="2026-10-01T00:00:00"),
          person("Rahul Jain", "Brand Manager"),
          person("Elsewhere", "Head of Partnerships", company_id=999),
          person("Tara Singh", "Brand Partnerships Intern"),
          person("Vikram Sethi", "Head of Partnerships")]


class Client:
    def __init__(self, profiles=PEOPLE):
        self.profiles, self.calls = profiles, 0

    def call(self, path, body):
        self.calls += 1
        return ApiResponse({"profiles": self.profiles}, "2026-11-22T10:00:00+00:00", False, 0.3)


class FindContactsTest(unittest.TestCase):
    def test_lists_everyone_who_qualifies_best_role_first(self):
        found = outreach.find_contacts(Client(), COMPANY, CFG)
        # Skipped: left the company, current job elsewhere, an intern title.
        self.assertEqual([(c.name, c.title) for c in found],
                         [("Vikram Sethi", "Head of Partnerships"), ("Rahul Jain", "Brand Manager"),
                          ("Meera Nair", "Marketing Manager")])
        self.assertTrue(all(c.business_email is None and c.evidence[0].record_date == "2024-01-01" for c in found))

    def test_find_contact_is_still_the_first_of_the_list(self):
        self.assertEqual(outreach.find_contact(Client(), COMPANY, CFG).name, "Vikram Sethi")
        self.assertEqual(outreach.find_contacts(Client([]), COMPANY, CFG), [])


class ChosenContactTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")
        self.client = Client()
        self.job = agent.Agent(RUN, False, self.db, self.client, {"outreach": CFG}, False)

    def tearDown(self):
        self.db.close()

    def test_contact_list_is_looked_up_once(self):
        first = agent.find_contacts(self.job, COMPANY)
        again = agent.find_contacts(self.job, COMPANY)
        self.assertEqual([c.name for c in first], [c.name for c in again])
        self.assertEqual(self.client.calls, 1)

    def test_the_contact_the_host_picks_is_the_one_block_6_pitches(self):
        people = agent.find_contacts(self.job, COMPANY)
        picked = people[1]                                   # not the top-ranked person
        sender = Sender()
        outreach.start_campaign(self.db, RUN, BRIEF, [(COMPANY, picked)], CFG, sender,
                                host_check=HOST_OK, ask=lambda _: "y", show=lambda _: None)

        self.assertEqual(agent.pitched_contact(self.job, COMPANY.company_id).name, "Rahul Jain")
        self.assertEqual(outreach.pipeline(self.db, RUN)[0]["contact"], "Rahul Jain, Brand Manager")
        draft = outreach.saved_pitch(self.db, RUN, COMPANY, BRIEF)
        self.assertEqual(draft.to_role, "Brand Manager, Example Corp")
        self.assertIn("Hi Rahul,", draft.body)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(sender.sent[0]["To"], CFG["team_inbox"])   # still only the team inbox

    def test_pitched_contact_stays_fixed_after_drafting(self):
        people = agent.find_contacts(self.job, COMPANY)
        quiet = {"host_check": HOST_OK, "ask": lambda _: "n", "show": lambda _: None}
        outreach.start_campaign(self.db, RUN, BRIEF, [(COMPANY, people[2])], CFG, Sender(), **quiet)
        outreach.start_campaign(self.db, RUN, BRIEF, [(COMPANY, people[0])], CFG, Sender(), **quiet)
        self.assertEqual(agent.pitched_contact(self.job, COMPANY.company_id).name, "Meera Nair")

    def test_no_one_picked_addresses_the_sponsorship_team(self):
        outreach.start_campaign(self.db, RUN, BRIEF, [(COMPANY, None)], CFG, Sender(),
                                host_check=HOST_OK, ask=lambda _: "n", show=lambda _: None)
        self.assertIsNone(agent.pitched_contact(self.job, COMPANY.company_id))
        self.assertEqual(outreach.saved_pitch(self.db, RUN, COMPANY, BRIEF).to_role,
                         "sponsorship team, Example Corp")

    def test_company_without_a_pitch_has_no_contact_yet(self):
        self.assertIsNone(agent.pitched_contact(self.job, COMPANY.company_id))


if __name__ == "__main__":
    unittest.main()
