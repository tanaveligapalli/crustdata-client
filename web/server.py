"""The web screen for the sponsorship agent: `python web/server.py [--debug] [--offline]`.

A small standard-library server. The page (web/static) shows the work; agent.py does it.

Rules this file keeps:
- Reading never pays. GET requests read finished stages from the `steps` table and rank
  them with scoring.rank; only POSTs run agent stages, in a background thread, one job
  per run at a time. Stages are steps.run_step, so a restart or a second click replays
  what's done instead of paying again.
- Weights never reach a normal user. Scores, confidence, signal values, weights, costs and
  evidence values are only in the response when the server was started with --debug AND
  the page asks for them (?debug=1). Without --debug the page has no toggle at all.
- Listens on 127.0.0.1 by default: Run spends credits and Send sends email.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
import threading
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import agent_db  # noqa: E402
import outreach  # noqa: E402
import scoring  # noqa: E402
from records import Brief, Candidate, Evidence, brief_from_dict, candidate_from_dict  # noqa: E402

SHOWN = 30            # ranked sponsors on the page (and logos looked up)
CREDIT_LIMIT = 150    # per run, like agent.open_agent; --credit-limit changes it. A first university
                      # or NGO run can pass 60: post pools ~37, alumni up to 40 x 0.23, CSR up to 15 x 2-3.
FOUND_BY = {
    "past_sponsor": "Sponsored similar events",
    "host_past_sponsor": "Sponsored your past events",
    "lookalike": "Like your past sponsors",
    "hiring": "Hiring brand & sponsorship roles",
    "company_search": "In your event's industries",
}
SIGNAL_LABELS = {
    "past_sponsorships": "Past sponsorships", "alumni": "Alumni", "csr": "CSR spending",
    "company_size": "Company size", "funding": "Recent funding", "listed": "Listed company",
    "social_presence": "Social following", "active_hiring": "Hiring now", "growth": "Headcount growth",
    "city_presence": "In your city",
}


# -- reading finished stages (never runs anything) -------------------------------


def step_output(db: sqlite3.Connection, run_id: str, name: str) -> Any:
    row = db.execute("SELECT output_json FROM steps WHERE run_id = ? AND name = ?", (run_id, name)).fetchone()
    return json.loads(row[0]) if row else None


def ensure_tables(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS web_runs (        -- the description behind each run started here
        run_id      TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS company_logos (   -- Crustdata's logo URL; NULL = it has none
        company_id INTEGER PRIMARY KEY,
        url        TEXT,
        checked_at TEXT NOT NULL
    );
    """)
    db.commit()


def ranked_candidates(db: sqlite3.Connection, run_id: str, brief: Brief,
                      scoring_cfg: dict[str, Any]) -> tuple[list[Candidate], list[str]] | None:
    data = step_output(db, run_id, "candidates")
    if data is None:
        return None
    found = [candidate_from_dict(c) for c in data["candidates"]]
    return scoring.rank(found, brief, scoring_cfg), data["failed_pools"]


# -- what the page receives ----------------------------------------------------------


def public_fact(f: Evidence) -> dict[str, Any]:
    """A fact as a normal user sees it: claim, quote, source and date. Never `value`
    (for post facts it holds the name-matching confidence score)."""
    return {"claim": f.claim, "quote": f.quote, "source_url": f.source_url, "source_type": f.source_type,
            "date": (f.record_date or "")[:10] or None, "status": f.status, "missing": f.missing}


def public_candidate(c: Candidate, position: int) -> dict[str, Any]:
    facts = [f for s in c.signals for f in s.evidence]
    facts.sort(key=lambda f: f.status != "verified")
    return {
        "position": position, "company_id": c.company_id, "name": c.name, "domain": c.domain,
        "industries": c.industries[:3],
        "found_by": [FOUND_BY.get(p, p.replace("_", " ")) for p in c.found_by],
        "facts": [public_fact(f) for f in facts],
        "verified_facts": sum(f.status == "verified" for f in facts),
        "excluded_reason": c.excluded_reason,
    }


def debug_candidate(c: Candidate, weights: dict[str, float]) -> dict[str, Any]:
    """Operator view: how the score was made. Only with --debug."""
    by_name = {s.name: s for s in c.signals}
    rows = []
    for name, weight in weights.items():
        s = by_name.get(name)
        known = bool(s and s.value is not None and any(e.status == "verified" for e in s.evidence))
        rows.append({"name": name, "label": SIGNAL_LABELS.get(name, name), "weight": weight,
                     "value": s.value if s else None, "known": known,
                     "contribution": round(weight * s.value, 4) if known else 0.0})
    return {"evidence_score": scoring.evidence_score(c), "score": c.score, "confidence": c.confidence,
            "signals": rows,
            "fact_values": [f.value for s in c.signals for f in s.evidence]}


def public_brief(b: Brief) -> dict[str, Any]:
    return {"event_name": b.event_name, "profile": b.profile, "host_type": b.host_type,
            "event_date": b.event_date, "city": b.city, "audience_size": b.audience_size,
            "audience": b.audience, "host_name": b.host_name, "host_website": b.host_website,
            "tiers": [{"name": t.name, "price_inr": t.price_inr, "benefits": t.benefits} for t in b.tiers],
            "current_sponsors": b.current_sponsors, "past_sponsors": b.past_sponsors,
            "excluded_categories": b.excluded_categories}


def run_view(db: sqlite3.Connection, run_id: str, scoring_cfg: dict[str, Any], debug: bool,
             job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the page shows for one run, from finished stages only."""
    ensure_tables(db)
    view: dict[str, Any] = {"run_id": run_id, "job": job, "brief": None, "dropped": [], "host": None,
                            "sponsors": [], "excluded": [], "failed_pools": [], "total_found": 0,
                            "pitches": [], "pipeline": []}
    brief_data = step_output(db, run_id, "brief")
    if brief_data is None:
        return view
    brief = brief_from_dict(brief_data["brief"])
    view["brief"] = public_brief(brief)
    view["dropped"] = [{"field": d["field"], "reason": d["reason"]} for d in brief_data["dropped"]]

    host = step_output(db, run_id, "host_check")
    if host is not None:
        view["host"] = public_fact(Evidence(**host))

    ranked = ranked_candidates(db, run_id, brief, scoring_cfg)
    if ranked is not None:
        candidates, failed = ranked
        weights = scoring_cfg["weights"][brief.host_type or scoring_cfg["default_host_type"]]
        logos = logo_urls(db, [c.company_id for c in candidates])
        shown = [c for c in candidates if c.excluded_reason is None][:SHOWN]
        view["total_found"] = len(candidates)
        view["failed_pools"] = failed
        view["sponsors"] = []
        for i, c in enumerate(shown, 1):
            item = {**public_candidate(c, i), "logo": logos.get(c.company_id)}
            if debug:
                item["debug"] = debug_candidate(c, weights)
            view["sponsors"].append(item)
        view["excluded"] = [{"name": c.name, "reason": c.excluded_reason}
                            for c in candidates if c.excluded_reason]
        by_id = {c.company_id: c for c in candidates}
        drafted = [r[0] for r in db.execute("SELECT company_id FROM pitch_drafts WHERE run_id = ?", (run_id,))] \
            if _has_table(db, "pitch_drafts") else []
        for company_id in drafted:
            d = outreach.saved_pitch(db, run_id, by_id[company_id], brief) if company_id in by_id else None
            if d:
                view["pitches"].append({"company_id": d.company_id, "company": d.company_name, "to_role": d.to_role,
                                        "subject": d.subject, "body": d.body, "notes": d.notes})
        view["pipeline"] = outreach.pipeline(db, run_id)

    if debug:
        view["debug"] = {"host_type": brief.host_type or scoring_cfg["default_host_type"],
                         "weights": scoring_cfg["weights"][brief.host_type or scoring_cfg["default_host_type"]],
                         "cost": cost(db, run_id),
                         "dropped": brief_data["dropped"]}
    return view


def cost(db: sqlite3.Connection, run_id: str) -> dict[str, float]:
    credits, usd = db.execute("SELECT COALESCE(SUM(credits), 0), COALESCE(SUM(cost_usd), 0) FROM ledger "
                              "WHERE run_id = ?", (run_id,)).fetchone()
    return {"credits": round(credits, 2), "model_usd": round(usd, 4)}


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def recent_runs(db: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_tables(db)
    out = []
    for run_id, created_at in db.execute("SELECT run_id, created_at FROM web_runs ORDER BY created_at DESC LIMIT 8"):
        data = step_output(db, run_id, "brief")
        out.append({"run_id": run_id, "created_at": created_at,
                    "event_name": data["brief"]["event_name"] if data else None})
    return out


# -- logos -----------------------------------------------------------------------------


def logo_urls(db: sqlite3.Connection, company_ids: list[int]) -> dict[int, str | None]:
    ensure_tables(db)
    if not company_ids:
        return {}
    marks = ",".join("?" * len(company_ids))
    return {r[0]: r[1] for r in db.execute(f"SELECT company_id, url FROM company_logos WHERE company_id IN ({marks})",
                                           company_ids)}


def fetch_logos(db: sqlite3.Connection, client: Any, company_ids: list[int]) -> int:
    """Look up Crustdata's logo for companies not checked yet (0.03 credits each, 22 Nov).
    A separate request, so the pools' cached requests are untouched. Returns how many were looked up."""
    ensure_tables(db)
    known = set(logo_urls(db, company_ids))
    missing = sorted(set(company_ids) - known)
    for i in range(0, len(missing), 100):
        chunk = missing[i:i + 100]
        response = client.call("/company/search", {
            "filters": {"field": "crustdata_company_id", "type": "in", "value": chunk},
            "fields": ["crustdata_company_id", "basic_info.logo_permalink"], "limit": len(chunk)})
        found = {c["crustdata_company_id"]: (c.get("basic_info") or {}).get("logo_permalink") or None
                 for c in response.data.get("companies") or []}
        for company_id in chunk:
            db.execute("INSERT OR REPLACE INTO company_logos VALUES (?, ?, ?)",
                       (company_id, found.get(company_id), agent_db.now_iso()))
        db.commit()
    return len(missing)


# -- background jobs: one per run at a time --------------------------------------------


class Jobs:
    """What is running right now, and the last error per run. Results live in the database."""

    def __init__(self, credit_limit: float = CREDIT_LIMIT) -> None:
        self.credit_limit = credit_limit
        self.lock = threading.Lock()
        self.running: dict[str, str] = {}     # run_id -> what it's doing, for the page
        self.errors: dict[str, dict] = {}

    def start(self, run_id: str, label: str, work: Callable[[Callable[[str], None]], None]) -> bool:
        """Start `work(say)` in a thread. False if this run already has a job going."""
        with self.lock:
            if run_id in self.running:
                return False
            self.running[run_id] = label
            self.errors.pop(run_id, None)

        def say(text: str) -> None:
            self.running[run_id] = text

        def main() -> None:
            try:
                work(say)
            except Exception as e:  # shown on the page; the run can be resumed
                self.errors[run_id] = error_view(e, self.credit_limit)
                traceback.print_exc()
            finally:
                with self.lock:
                    self.running.pop(run_id, None)

        threading.Thread(target=main, daemon=True).start()
        return True

    def status(self, run_id: str) -> dict[str, Any]:
        return {"running": self.running.get(run_id), "error": self.errors.get(run_id)}


def error_view(e: Exception, credit_limit: float = CREDIT_LIMIT) -> dict[str, Any]:
    from brief import BriefIncomplete, ExtractionFailed
    from crustdata_client import BudgetExceeded, NotInCache

    if isinstance(e, BriefIncomplete):
        return {"kind": "incomplete", "missing": e.missing,
                "message": "Please add to the description: " + ", ".join(m.replace("_", " ") for m in e.missing) + "."}
    if isinstance(e, ExtractionFailed):
        return {"kind": "unreadable", "message": "We couldn't read the description. Try rewording it."}
    if isinstance(e, (agent.NotCached, NotInCache)):
        return {"kind": "not_cached", "message": "This step was never run online, so there's nothing to replay."}
    if isinstance(e, BudgetExceeded):
        # Resuming at the same limit fails at once: the ledger counts what the run spent before.
        return {"kind": "budget", "message": f"Stopped at this run's limit of {credit_limit:g} credits. To finish it, "
                "restart the server with a higher --credit-limit and search again: finished steps and "
                "cached calls cost nothing."}
    if isinstance(e, outreach.OutreachBlocked):
        return {"kind": "blocked", "message": str(e)}
    return {"kind": "error", "message": f"Something went wrong: {type(e).__name__}. Try again; finished steps are kept."}


# -- the work behind each POST ---------------------------------------------------------


class Workers:
    """Each job opens its own agent (and SQLite connections) inside its own thread."""

    def __init__(self, offline: bool, open_agent: Callable[..., Any] = agent.open_agent,
                 credit_limit: float = CREDIT_LIMIT) -> None:
        self.offline = offline
        self.open_agent = open_agent
        self.credit_limit = credit_limit

    def _agent(self, description: str):
        return self.open_agent(description, offline=self.offline, credit_limit=self.credit_limit)

    def search(self, description: str, say: Callable[[str], None]) -> None:
        job = self._agent(description)
        try:
            say("Reading your description")
            brief, _ = agent.make_brief(job, description)
            say("Checking the host")
            agent.check_host(job, brief)
            say("Finding sponsors: job posts, past sponsorships, company data (a few minutes the first time)")
            ranked, _ = agent.find_candidates(job, brief)
            say("Getting logos")
            shown = [c.company_id for c in ranked if c.excluded_reason is None][:SHOWN]
            try:
                fetch_logos(job.db, job.client, shown)
            except Exception:   # logos are decoration: never fail a run over them
                traceback.print_exc()
        finally:
            _close(job)

    def pitches(self, description: str, company_ids: list[int], say: Callable[[str], None]) -> None:
        job = self._agent(description)
        try:
            brief, _ = agent.make_brief(job, description)
            host = agent.check_host(job, brief)
            ranked, _ = agent.find_candidates(job, brief)
            chosen = [c for c in ranked if c.company_id in set(company_ids) and c.excluded_reason is None]
            say("Finding the decision-maker at each company")
            selected = [(c, agent.find_contact(job, c)) for c in chosen]
            say("Drafting pitches from your brief and the sourced facts")
            agent.draft_pitches(job, brief, host, selected)
        finally:
            _close(job)

    def send(self, description: str, company_ids: list[int], say: Callable[[str], None]) -> None:
        job = self._agent(description)
        try:
            brief, _ = agent.make_brief(job, description)
            host = agent.check_host(job, brief)
            ranked, _ = agent.find_candidates(job, brief)
            drafted = {r[0] for r in job.db.execute("SELECT company_id FROM pitch_drafts WHERE run_id = ?",
                                                    (job.run_id,))}
            selected = [(c, agent.find_contact(job, c)) for c in ranked if c.company_id in drafted]
            say("Sending the approved pitches")
            agent.send_approved(job, brief, host, selected, set(company_ids) & drafted)
        finally:
            _close(job)


def _close(job: Any) -> None:
    for thing in (getattr(job, "client", None), getattr(job, "db", None)):
        try:
            thing.close()
        except Exception:
            pass


# -- HTTP ------------------------------------------------------------------------------


class App:
    def __init__(self, debug: bool = False, offline: bool = False, db_path: str | Path = agent_db.DEFAULT_PATH,
                 workers: Workers | None = None, credit_limit: float = CREDIT_LIMIT) -> None:
        self.debug = debug
        self.offline = offline
        self.db_path = str(db_path)
        self.credit_limit = credit_limit
        self.jobs = Jobs(credit_limit)
        self.workers = workers or Workers(offline, credit_limit=credit_limit)
        self.scoring_cfg = scoring.load_config()

    def db(self) -> sqlite3.Connection:
        return agent_db.connect(self.db_path)

    def description(self, db: sqlite3.Connection, run_id: str) -> str | None:
        ensure_tables(db)
        row = db.execute("SELECT description FROM web_runs WHERE run_id = ?", (run_id,)).fetchone()
        return row[0] if row else None

    def handle(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> tuple[int, Any]:
        """(status, JSON-able) for every /api/ route."""
        parts = [p for p in path.split("/") if p][1:]          # drop "api"
        want_debug = self.debug and query.get("debug", ["0"])[0] == "1"
        db = self.db()
        try:
            if method == "GET" and parts == ["config"]:
                return 200, {"debug_available": self.debug, "offline": self.offline,
                             "credit_limit": self.credit_limit, "email": email_destination(), "examples": examples()}
            if method == "GET" and parts == ["runs"]:
                return 200, recent_runs(db)
            if method == "POST" and parts == ["runs"]:
                description = (body.get("description") or "").strip()
                if not description:
                    return 400, {"error": "Describe your event first."}
                run_id = agent.run_id_for(description)
                ensure_tables(db)
                db.execute("INSERT OR IGNORE INTO web_runs VALUES (?, ?, ?)", (run_id, description, agent_db.now_iso()))
                db.commit()
                self.jobs.start(run_id, "Starting", lambda say: self.workers.search(description, say))
                return 202, {"run_id": run_id}
            if len(parts) >= 2 and parts[0] == "runs":
                run_id = parts[1]
                description = self.description(db, run_id)
                if description is None:
                    return 404, {"error": "No such run on this screen."}
                if method == "GET" and len(parts) == 2:
                    return 200, run_view(db, run_id, self.scoring_cfg, want_debug, self.jobs.status(run_id))
                if method == "POST" and len(parts) == 3 and parts[2] in ("search", "pitches", "send"):
                    return self.action(run_id, description, parts[2], body)
            return 404, {"error": "Not found"}
        finally:
            db.close()

    def action(self, run_id: str, description: str, kind: str, body: dict[str, Any]) -> tuple[int, Any]:
        ids = [int(i) for i in body.get("company_ids") or []]
        if kind == "search":
            work = lambda say: self.workers.search(description, say)  # noqa: E731  (resume after a restart)
        elif self.offline:
            return 409, {"error": "Replay mode: pitches and sending need a live server."}
        elif not ids:
            return 400, {"error": "Pick at least one company."}
        elif kind == "pitches":
            work = lambda say: self.workers.pitches(description, ids, say)  # noqa: E731
        else:
            work = lambda say: self.workers.send(description, ids, say)  # noqa: E731
        if not self.jobs.start(run_id, "Starting", work):
            return 409, {"error": "Already working on this run.", **self.jobs.status(run_id)}
        return 202, {"run_id": run_id}


def email_destination() -> str:
    """Where approved emails go, so the page can say so before anyone presses Send."""
    try:
        outreach.load_config()
    except ValueError:
        return "outbox"
    return "team_inbox" if os.environ.get("OUTREACH_APP_PASSWORD") else "outbox"


def examples() -> list[dict[str, str]]:
    return [{"name": p.stem.replace("_", " "), "text": p.read_text(encoding="utf-8")}
            for p in sorted((ROOT / "demo").glob("*.txt"))]


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                body = {}
                if method == "POST" and not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    # Browsers won't send JSON cross-site without asking, so another website
                    # open in the same browser can't start a paid search here.
                    return self._send(415, {"error": "Send JSON"})
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except json.JSONDecodeError:
                        return self._send(400, {"error": "Bad JSON"})
                status, payload = app.handle(method, url.path, parse_qs(url.query), body)
                return self._send(status, payload)
            if method != "GET":
                return self._send(405, {"error": "Method not allowed"})
            self._static(url.path)

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (STATIC / name).resolve()
            if not file.is_file() or STATIC.resolve() not in file.parents:
                return self._send(404, {"error": "Not found"})
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                             + ("; charset=utf-8" if file.suffix in (".html", ".css", ".js", ".svg") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter: only errors
            if args and str(args[1]).startswith(("4", "5")):
                super().log_message(fmt, *args)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this machine only (default)")
    parser.add_argument("--debug", action="store_true", help="allow the debug toggle (weights, scores, costs)")
    parser.add_argument("--offline", action="store_true", help="replay from the cache: no API or model calls")
    parser.add_argument("--db", default=str(agent_db.DEFAULT_PATH), help="the agent's database (default agent.sqlite3)")
    parser.add_argument("--credit-limit", type=float, default=CREDIT_LIMIT, help=f"credits per run (default {CREDIT_LIMIT})")
    args = parser.parse_args()
    app = App(debug=args.debug, offline=args.offline, db_path=args.db, credit_limit=args.credit_limit)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    mode = ", ".join(m for m, on in (("debug", args.debug), ("replay", args.offline)) if on) or "normal"
    print(f"Sponsor Agent on http://{args.host}:{args.port} ({mode}). Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
