"""Restart safety: steps run once, actions happen once, a crashed action waits for a person."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
from steps import ActionNeedsCheck, do_once, run_step  # noqa: E402


class StepsTest(unittest.TestCase):
    def setUp(self):
        self.db = agent_db.connect(":memory:")

    def test_finished_step_is_not_run_again_after_restart(self):
        runs = []

        def work():
            runs.append(1)
            return {"candidates": 3}

        self.assertEqual(run_step(self.db, "run-1", "find_candidates", work), {"candidates": 3})
        self.assertEqual(run_step(self.db, "run-1", "find_candidates", work), {"candidates": 3})
        self.assertEqual(len(runs), 1)

    def test_action_happens_once(self):
        sent = []

        def send():
            sent.append(1)
            return {"message_id": "m1"}

        do_once(self.db, "run-1", "email:acme:intro", "email", send)
        self.assertEqual(do_once(self.db, "run-1", "email:acme:intro", "email", send), {"message_id": "m1"})
        self.assertEqual(len(sent), 1)

    def test_action_interrupted_by_a_crash_waits_for_a_person(self):
        def killed_mid_send():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            do_once(self.db, "run-1", "email:acme:intro", "email", killed_mid_send)

        with self.assertRaises(ActionNeedsCheck):
            do_once(self.db, "run-1", "email:acme:intro", "email", lambda: {"message_id": "m2"})


if __name__ == "__main__":
    unittest.main()
