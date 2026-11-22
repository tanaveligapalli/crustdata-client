"""The static copy for Netlify, offline: a seeded database, a temporary output folder."""

import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import agent  # noqa: E402
import agent_db  # noqa: E402
import export_site  # noqa: E402
from records import Brief, Candidate  # noqa: E402
from steps import run_step  # noqa: E402


class ExportSiteTest(unittest.TestCase):
    def test_only_events_that_ran_are_exported_without_contacts(self):
        text = (ROOT / "demo" / "fintech_summit.txt").read_text(encoding="utf-8")
        run_id = agent.run_id_for(text)
        brief = Brief("FinTech Forward Summit 2027", "conference", "2027-02-20", "Bengaluru", 800, "founders",
                      "Razorpay", "razorpay.com", host_type="company")
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "agent.sqlite3"
            db = agent_db.connect(db_path)
            run_step(db, run_id, "brief", lambda: {"brief": asdict(brief), "dropped": []})
            run_step(db, run_id, "candidates",
                     lambda: {"candidates": [asdict(Candidate(7, "Acme", "acme.in"))], "failed_pools": []})
            db.close()

            names = export_site.export(Path(folder) / "site", db_path)

            site = Path(folder) / "site"
            self.assertEqual(names, ["fintech summit"])                 # the others never ran here
            config = json.loads((site / "api" / "config.json").read_text(encoding="utf-8"))
            self.assertTrue(config["static"] and config["offline"])
            self.assertEqual(config["examples"][0]["run_id"], run_id)
            view = json.loads((site / "api" / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
            self.assertEqual((view["pitches"], view["pipeline"]), ([], []))
            self.assertIn("/api/runs/*", (site / "_redirects").read_text(encoding="utf-8"))
            self.assertTrue((site / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()
