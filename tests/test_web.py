"""The web screen, offline: a temp database and fake workers; no API, model or email."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import agent  # noqa: E402
import agent_db  # noqa: E402
import server  # noqa: E402
from records import Brief, Candidate, Evidence, Signal, Tier  # noqa: E402

DESCRIPTION = "TechNova Summit 2027, a tech conference in Bengaluru, hosted by TechNova Foundation."
RUN = agent.run_id_for(DESCRIPTION)
# Keys that would leak how the ranking was made. Normal users must never receive them.
SECRET_KEYS = {"score", "confidence", "evidence_score", "weight", "weights", "value", "contribution",
               "cost", "credits", "model_usd", "fact_values", "debug", "signals"}


def fact(claim, status="verified"):
    return Evidence(claim, {"crustdata_company_id": 7, "confidence_score": 1.0}, "https://example.in/post", "q",
                    "2026-09-04" if status == "verified" else None, "2026-11-22T09:00:00+00:00", "linkedin_post",
                    status)


def seed(db):
    """Finished stages for one run, as agent.py saves them."""
    brief = Brief(event_name="TechNova Summit 2027", profile="conference", event_date="2027-03-14", city="Bengaluru",
                  audience_size=2000, audience="founders", host_name="TechNova Foundation", host_website="technova.org",
                  tiers=[Tier("Title", 500000, ["main stage"])], host_type="university")
    candidates = [
        Candidate(7, "Acme", "acme.in", found_by=["past_sponsor"], industries=["Software Development"],
                  signals=[Signal("past_sponsorships", 1.0, [fact("Acme sponsored DevFest 2026")]),
                           Signal("company_size", 0.5, [fact("Acme has 500 staff")])]),
        Candidate(8, "Brewco", "brew.co", found_by=["hiring"], industries=["Beer"],
                  signals=[Signal("active_hiring", 0.3, [fact("Brewco is hiring a brand manager")])]),
    ]
    host = fact("TechNova Foundation is a real organization")
    rows = {"brief": {"brief": asdict(brief), "dropped": [{"field": "city", "value": "x", "reason": "not in text"}]},
            "host_check": asdict(host),
            "candidates": {"candidates": [asdict(c) for c in candidates], "failed_pools": []}}
    for name, output in rows.items():
        db.execute("INSERT INTO steps VALUES (?, ?, ?, ?)", (RUN, name, json.dumps(output), "2026-11-22T09:00:00+00:00"))
    server.ensure_tables(db)
    db.execute("INSERT INTO web_runs VALUES (?, ?, ?)", (RUN, DESCRIPTION, "2026-11-22T09:00:00+00:00"))
    db.commit()


def keys_in(payload):
    if isinstance(payload, dict):
        return set(payload) | {k for v in payload.values() for k in keys_in(v)}
    if isinstance(payload, list):
        return {k for v in payload for k in keys_in(v)}
    return set()


class FakeWorkers:
    """Records calls; `gate` holds a job open so a second click can be tested."""

    def __init__(self):
        self.calls, self.gate = [], threading.Event()

    def search(self, description, say):
        self.calls.append("search")
        self.gate.wait(5)

    def pitches(self, description, ids, say):
        self.calls.append(("pitches", ids))

    def send(self, description, ids, say):
        self.calls.append(("send", ids))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agent.sqlite3"
        db = agent_db.connect(self.path)
        seed(db)
        db.close()
        self.workers = FakeWorkers()

    def tearDown(self):
        self.workers.gate.set()
        self.tmp.cleanup()

    def app(self, **kw):
        return server.App(db_path=self.path, workers=self.workers, **kw)


class DebugGateTests(Base):
    def test_normal_users_never_receive_weights_scores_or_costs(self):
        for app in (self.app(), self.app(debug=False)):
            for query in ({}, {"debug": ["1"]}):          # asking for debug changes nothing without --debug
                status, view = app.handle("GET", f"/api/runs/{RUN}", query, {})
                self.assertEqual(status, 200)
                leaked = {k for k in keys_in(view) if k in SECRET_KEYS or "score" in k or "confidence" in k}
                self.assertEqual(leaked, set(), query)
        self.assertEqual(len(view["sponsors"]), 2)
        self.assertEqual(view["sponsors"][0]["name"], "Acme")       # still ranked
        self.assertEqual(view["sponsors"][0]["facts"][0]["claim"], "Acme sponsored DevFest 2026")

    def test_debug_server_shows_weights_only_when_asked(self):
        app = self.app(debug=True)
        _, plain = app.handle("GET", f"/api/runs/{RUN}", {}, {})
        self.assertNotIn("debug", keys_in(plain))
        _, view = app.handle("GET", f"/api/runs/{RUN}", {"debug": ["1"]}, {})
        acme = view["sponsors"][0]["debug"]
        self.assertGreater(acme["evidence_score"], 0)
        weight = {r["name"]: r["weight"] for r in acme["signals"]}["past_sponsorships"]
        self.assertEqual(weight, view["debug"]["weights"]["past_sponsorships"])
        self.assertIn("credits", view["debug"]["cost"])

    def test_config_tells_the_page_whether_a_toggle_exists(self):
        self.assertFalse(self.app().handle("GET", "/api/config", {}, {})[1]["debug_available"])
        self.assertTrue(self.app(debug=True).handle("GET", "/api/config", {}, {})[1]["debug_available"])


class ReadingNeverPaysTests(Base):
    def test_get_never_runs_a_stage(self):
        def boom(*_a, **_k):
            raise AssertionError("a GET ran an agent stage")

        with mock.patch.multiple(agent, make_brief=boom, check_host=boom, find_candidates=boom,
                                 find_contact=boom, draft_pitches=boom, send_approved=boom, open_agent=boom):
            app = self.app(debug=True)
            for path in ("/api/config", "/api/runs", f"/api/runs/{RUN}"):
                self.assertEqual(app.handle("GET", path, {"debug": ["1"]}, {})[0], 200, path)
        self.assertEqual(self.workers.calls, [])


class JobTests(Base):
    def test_a_second_click_while_running_starts_nothing(self):
        app = self.app()
        self.assertEqual(app.handle("POST", f"/api/runs/{RUN}/search", {}, {})[0], 202)
        self.assertEqual(app.handle("POST", f"/api/runs/{RUN}/search", {}, {})[0], 409)
        self.assertEqual(app.handle("POST", "/api/runs", {}, {"description": DESCRIPTION})[0], 202)
        self.workers.gate.set()
        self.assertEqual(self.workers.calls, ["search"])

    def test_replay_mode_never_drafts_or_sends(self):
        app = self.app(offline=True)
        for kind in ("pitches", "send"):
            self.assertEqual(app.handle("POST", f"/api/runs/{RUN}/{kind}", {}, {"company_ids": [7]})[0], 409)
        self.assertEqual(self.workers.calls, [])

    def test_send_needs_explicit_companies(self):
        self.assertEqual(self.app().handle("POST", f"/api/runs/{RUN}/send", {}, {"company_ids": []})[0], 400)


class BudgetTests(unittest.TestCase):
    def test_a_budget_stop_says_how_to_continue_not_resume(self):
        from crustdata_client import BudgetExceeded
        view = server.error_view(BudgetExceeded("Run x has spent 150 of 150 credits"), 150)
        self.assertEqual(view["kind"], "budget")            # the page offers no Resume for this kind
        self.assertIn("higher --credit-limit", view["message"])
        self.assertIn("150", view["message"])

    def test_the_agent_uses_the_servers_database(self):
        seen = []

        def fake_open_agent(description, offline, credit_limit, db_path):
            seen.append(db_path)
            raise RuntimeError("stop here")

        workers = server.Workers(False, open_agent=fake_open_agent, credit_limit=40, db_path="/tmp/other.sqlite3")
        for work in (lambda: workers.search("x", lambda _: None), lambda: workers.quick("x", lambda job: None)):
            with self.assertRaises(RuntimeError):
                work()
        self.assertEqual(seen, ["/tmp/other.sqlite3", "/tmp/other.sqlite3"])
        self.assertEqual(server.App(db_path=":memory:").workers.db_path, ":memory:")

    def test_the_limit_reaches_the_page_and_the_agent(self):
        app = server.App(db_path=":memory:", credit_limit=40)
        self.assertEqual(app.handle("GET", "/api/config", {}, {})[1]["credit_limit"], 40)
        self.assertEqual(app.workers.credit_limit, 40)


class LogoTests(Base):
    class Client:
        def __init__(self):
            self.calls = 0

        def call(self, path, body):
            from crustdata_client import ApiResponse
            self.calls += 1
            companies = [{"crustdata_company_id": 7, "basic_info": {"logo_permalink": "https://cdn.example/acme.png"}}]
            return ApiResponse({"companies": companies}, "2026-11-22T09:00:00+00:00", False, 0.06)

    def test_logos_are_looked_up_once_and_a_missing_one_is_remembered(self):
        db = agent_db.connect(self.path)
        client = self.Client()
        self.assertEqual(server.fetch_logos(db, client, [7, 8]), 2)
        self.assertEqual(server.fetch_logos(db, client, [7, 8]), 0)
        self.assertEqual(client.calls, 1)
        self.assertEqual(server.logo_urls(db, [7, 8]), {7: "https://cdn.example/acme.png", 8: None})
        db.close()
        _, view = self.app().handle("GET", f"/api/runs/{RUN}", {}, {})
        self.assertEqual([s["logo"] for s in view["sponsors"]], ["https://cdn.example/acme.png", None])


class HttpTests(Base):
    def test_page_is_served_and_files_outside_static_are_not(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app()))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/") as r:
                self.assertIn(b"<!doctype html>", r.read().lower())
            for bad in ("/../config.yaml", "/%2e%2e/config.yaml", "/../../.env"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + bad)
                self.assertEqual(e.exception.code, 404, bad)
            # A form post from another website (not JSON) is refused before anything runs.
            form = urllib.request.Request(base + "/api/runs", data=b"description=x", method="POST",
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(form)
            self.assertEqual(e.exception.code, 415)
            self.assertEqual(self.workers.calls, [])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
