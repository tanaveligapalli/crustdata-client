"""The whole job, stage by stage: description -> brief -> host check -> candidates ->
ranking -> contacts -> pitches the host approves -> pipeline. app.py is the screen on top.

The run ID comes from the description, and every paid stage is a steps.run_step. So the
same description after a crash, or in the offline demo, replays finished stages from the
database: nothing is paid for or sent twice.

Replies are handled in a terminal with `python outreach.py replies RUN_ID BRIEF_JSON`,
where a person approves each answer or invite before it is sent.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
import sqlite3
from dataclasses import asdict, dataclass, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

import agent_db
import brief as brief_mod
import candidates as candidates_mod
import company_signals
import evidence
import host_check
import instagram_sponsors
import outreach
import scoring
from crustdata_client import CrustdataClient, SourceUnavailable
from records import Brief, Candidate, Contact, Evidence, brief_from_dict, candidate_from_dict, contact_from_dict
from src import extract
from steps import run_step

OUTBOX = Path(__file__).resolve().parent / "outbox"


class NotCached(Exception):
    """Offline demo: this step never ran online, so there is nothing to replay."""


@dataclass
class Agent:
    run_id: str
    offline: bool              # replay only: no API or model calls
    db: sqlite3.Connection
    client: CrustdataClient
    config: dict[str, Any]     # one section per block, from config.yaml
    email_ready: bool          # False: no Gmail settings, so emails are written to outbox/ instead


def run_id_for(description: str) -> str:
    """Same words, same run: spacing and line endings (Windows checkouts) don't matter."""
    return "run-" + hashlib.sha256(" ".join(description.split()).encode("utf-8")).hexdigest()[:10]


def open_agent(description: str, offline: bool = False, credit_limit: float = 150) -> Agent:
    run_id = run_id_for(description)
    db = agent_db.connect()
    client = CrustdataClient(run_id=run_id, ledger_db=db, credit_limit=credit_limit, offline=offline)
    outreach_config, email_ready = _outreach_config()
    config = {"brief": brief_mod.load_config(), "candidates": candidates_mod.load_config(),
              "instagram": instagram_sponsors.load_config(), "scoring": scoring.load_config(),
              "company_signals": _section("company_signals"), "outreach": outreach_config}
    return Agent(run_id, offline, db, client, config, email_ready)


# -- stages -------------------------------------------------------------------


def make_brief(agent: Agent, description: str) -> tuple[Brief, list[dict]]:
    """Raises brief.BriefIncomplete (ask the host) or brief.ExtractionFailed."""
    call = _model(agent, lambda: brief_mod.anthropic_caller(agent.config["brief"]))

    def work() -> dict:
        found, dropped = brief_mod.make_brief(description, agent.db, agent.run_id, agent.config["brief"], call)
        return {"brief": asdict(found), "dropped": dropped}

    data = run_step(agent.db, agent.run_id, "brief", work)
    return brief_from_dict(data["brief"]), data["dropped"]


def check_host(agent: Agent, brief: Brief) -> Evidence:
    data = run_step(agent.db, agent.run_id, "host_check",
                    lambda: asdict(host_check.check_host(brief, agent.client, agent.db, agent.run_id)))
    return Evidence(**data)


def find_candidates(agent: Agent, brief: Brief) -> tuple[list[Candidate], list[str]]:
    """Ranked candidates (excluded ones last, with the reason) and the sources that failed."""
    call = _model(agent, lambda: extract.anthropic_caller(agent.config["instagram"]))

    def work() -> dict:
        found, failed = candidates_mod.find_candidates(brief, agent.client, agent.db, agent.run_id,
                                                       agent.config["candidates"], agent.config["instagram"], call)
        found = add_company_signals(agent, brief, found, failed)
        return {"candidates": [asdict(c) for c in found], "failed_pools": failed}

    data = run_step(agent.db, agent.run_id, "candidates", work)
    found = [candidate_from_dict(c) for c in data["candidates"]]
    return scoring.rank(found, brief, agent.config["scoring"]), data["failed_pools"]


def add_company_signals(agent: Agent, brief: Brief, found: list[Candidate], failed: list[str]) -> list[Candidate]:
    """Company size, growth, funding, listing and social presence for every candidate; alumni
    and CSR (costlier) only when the host type weighs them, and only for the top companies.
    A source that keeps failing is added to `failed` and its signal stays unknown."""
    cfg = agent.config["company_signals"]
    weights = agent.config["scoring"]["weights"][brief.host_type or agent.config["scoring"]["default_host_type"]]

    def attach(lookup: Callable[[], dict[int, list]], source: str) -> None:
        nonlocal found
        try:
            extra = lookup()
        except SourceUnavailable:
            failed.append(source)
            return
        found = [replace(c, signals=c.signals + extra.get(c.company_id, [])) for c in found]
        for company_id, signals in extra.items():
            evidence.save(agent.db, agent.run_id, f"company:{company_id}", [f for s in signals for f in s.evidence])

    def top(n: int) -> list[Candidate]:
        ranked = scoring.rank(found, brief, agent.config["scoring"])
        return [c for c in ranked if c.excluded_reason is None][:n]

    attach(lambda: company_signals.company_facts([c.company_id for c in found], agent.client, cfg), "company data")
    if weights.get("alumni"):
        attach(lambda: company_signals.alumni(top(cfg["alumni_top"]), brief.host_name, agent.client, cfg), "alumni")
    if weights.get("csr"):
        attach(lambda: company_signals.csr(top(cfg["csr_top"]), agent.client), "CSR pages")
    return found


def find_contact(agent: Agent, candidate: Candidate) -> Contact | None:
    """Only for companies the host selected: each lookup costs credits."""
    def work() -> dict | None:
        contact = outreach.find_contact(agent.client, candidate, agent.config["outreach"])
        return asdict(contact) if contact else None

    data = run_step(agent.db, agent.run_id, f"contact:{candidate.company_id}", work)
    return contact_from_dict(data) if data else None


def find_contacts(agent: Agent, candidate: Candidate) -> list[Contact]:
    """Everyone at a selected company who could be pitched, best role first. The host picks one.

    One /person/search per company (the same request as find_contact), saved as a step,
    so showing the list again or after a restart costs nothing.
    """
    def work() -> list[dict]:
        return [asdict(c) for c in outreach.find_contacts(agent.client, candidate, agent.config["outreach"])]

    return [contact_from_dict(d) for d in run_step(agent.db, agent.run_id, f"contacts:{candidate.company_id}", work)]


def pitched_contact(agent: Agent, company_id: int) -> Contact | None:
    """The contact block 6 saved when this company's pitch was drafted. It stays fixed after that,
    because the pitch shown for approval is addressed to them."""
    outreach.ensure_tables(agent.db)
    row = outreach.get_row(agent.db, agent.run_id, company_id)
    if row is None or not row["contact_json"]:
        return None
    return contact_from_dict(json.loads(row["contact_json"]))


def draft_pitches(agent: Agent, brief: Brief, host: Evidence,
                  selected: list[tuple[Candidate, Contact | None]]) -> list[outreach.Draft]:
    """Draft (or reload) a pitch per company. Sends nothing new: every approval answer is "no".

    Raises outreach.OutreachBlocked when the host isn't verified.
    """
    outreach.start_campaign(agent.db, agent.run_id, brief, selected, agent.config["outreach"], _sender(agent),
                            model=_pitch_model(agent), host_check=host, ask=lambda _: "n", show=lambda _: None)
    return [d for c, _ in selected if (d := outreach.saved_pitch(agent.db, agent.run_id, c, brief))]


def send_approved(agent: Agent, brief: Brief, host: Evidence, selected: list[tuple[Candidate, Contact | None]],
                  approved_ids: set[int]) -> list[dict]:
    """Send the pitches the host ticked, exactly as drafted, each at most once."""
    names = {c.name for c, _ in selected if c.company_id in approved_ids}

    def ask(question: str) -> str:   # outreach asks "Send this intro to <company>? [y/N] "
        return "y" if any(question.endswith(f" to {name}? [y/N] ") for name in names) else "n"

    return outreach.start_campaign(agent.db, agent.run_id, brief, selected, agent.config["outreach"],
                                   _sender(agent), model=_pitch_model(agent), host_check=host,
                                   ask=ask, show=lambda _: None)


def pipeline(agent: Agent) -> list[dict]:
    return outreach.pipeline(agent.db, agent.run_id)


# -- replies ------------------------------------------------------------------
# With Gmail set up, replies are read from the team inbox. Without it (the demo), a person types
# the sponsor's reply into the screen's demo inbox, from any sender address they like. Both go
# through the same outreach.handle_replies: same labels, same once-only sends.


def reply_source(agent: Agent) -> str:
    return "gmail" if agent.email_ready else "demo"


def _ensure_demo_inbox(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS demo_inbox (
                      message_id TEXT PRIMARY KEY,
                      run_id     TEXT NOT NULL,
                      company_id INTEGER NOT NULL,
                      sender     TEXT NOT NULL,
                      subject    TEXT NOT NULL,
                      body       TEXT NOT NULL,
                      created_at TEXT NOT NULL)""")


def add_demo_reply(agent: Agent, company_id: int, sender: str, text: str) -> str:
    """A sponsor's reply typed into the demo inbox. Only to a company we've emailed this run."""
    outreach.ensure_tables(agent.db)
    row = outreach.get_row(agent.db, agent.run_id, company_id)
    if row is None or row["state"] in ("drafted", "approved"):
        raise ValueError("Only companies we've emailed can reply.")
    if "@" not in sender or not text.strip():
        raise ValueError("Give a sender address and the reply's text.")
    _ensure_demo_inbox(agent.db)
    message_id = f"<demo-{uuid.uuid4().hex}@demo.local>"
    subject = f"Re: [ref:{agent.run_id}:{company_id}] Sponsorship"   # the ref tag a real reply keeps
    agent.db.execute("INSERT INTO demo_inbox VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (message_id, agent.run_id, company_id, sender.strip(), subject, text.strip(), agent_db.now_iso()))
    agent.db.commit()
    return message_id


def _reply_reader(agent: Agent) -> Callable[[], list[outreach.Reply]]:
    if agent.email_ready:
        return outreach.imap_reader(agent.config["outreach"])

    def read() -> list[outreach.Reply]:
        _ensure_demo_inbox(agent.db)
        return [outreach.Reply(r[0], r[1], r[2], r[3]) for r in agent.db.execute(
            "SELECT message_id, sender, subject, body FROM demo_inbox WHERE run_id = ? ORDER BY created_at",
            (agent.run_id,))]
    return read


def check_replies(agent: Agent, brief: Brief) -> list[dict]:
    """Read new replies and sort each one. Declines, opt-outs and hand-offs to the host happen now;
    an answer or meeting invite to the sponsor waits for approval (held_replies)."""
    return outreach.handle_replies(agent.db, agent.run_id, brief, agent.config["outreach"], _reply_reader(agent),
                                   _sender(agent), model=_pitch_model(agent), hold=True)


def held_replies(agent: Agent) -> list[dict]:
    return outreach.held_drafts(agent.db, agent.run_id)


def send_held_replies(agent: Agent, ids: list[int]) -> int:
    return outreach.send_held(agent.db, agent.run_id, ids, agent.config["outreach"], _sender(agent))


def discard_held_replies(agent: Agent, ids: list[int]) -> None:
    outreach.discard_held(agent.db, agent.run_id, ids)


def reply_log(db: sqlite3.Connection, run_id: str) -> list[dict]:
    """Every reply handled this run, newest first: who it was from, its label and (demo) its text.
    Takes the database, not an Agent, so the web screen can read it without opening a client."""
    outreach.ensure_tables(db)
    _ensure_demo_inbox(db)
    rows = db.execute(
        """SELECT s.message_id, s.company_id, o.company_name, s.state, s.at, d.sender, d.body
           FROM replies_seen s JOIN outreach o ON o.run_id = s.run_id AND o.company_id = s.company_id
           LEFT JOIN demo_inbox d ON d.message_id = s.message_id
           WHERE s.run_id = ? ORDER BY s.at DESC""", (run_id,)).fetchall()
    return [{"company_id": r[1], "company": r[2], "label": r[3], "at": r[4], "sender": r[5], "text": r[6]}
            for r in rows]


def cost(agent: Agent) -> dict[str, float]:
    """What this job has cost so far, from the ledger."""
    credits, usd = agent.db.execute(
        "SELECT COALESCE(SUM(credits), 0), COALESCE(SUM(cost_usd), 0) FROM ledger WHERE run_id = ?",
        (agent.run_id,)).fetchone()
    return {"credits": round(credits, 2), "model_usd": round(usd, 4)}


# -- models and email ---------------------------------------------------------


def _model(agent: Agent, make: Callable[[], Callable]) -> Callable:
    if agent.offline:
        def offline(*_args):
            raise NotCached("offline replay: this model call was never made online")
        return offline
    return make()


def _pitch_model(agent: Agent) -> outreach.ModelCall | None:
    # Offline, pitches come back from the database; a missing one uses the plain template.
    if agent.offline or not agent.config["outreach"].get("model"):
        return None
    return outreach.anthropic_caller(agent.config["outreach"])


def _section(name: str) -> dict[str, Any]:
    import yaml

    with open(Path(__file__).resolve().parent / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)[name]


def _outreach_config() -> tuple[dict[str, Any], bool]:
    try:
        return outreach.load_config(), bool(os.environ.get("OUTREACH_APP_PASSWORD"))
    except ValueError:  # the team Gmail isn't set up on this machine
        import yaml

        with open(outreach.CONFIG_PATH, encoding="utf-8") as f:
            config = yaml.safe_load(f)["outreach"]
        config["sender"] = config["team_inbox"] = "team-inbox@outbox.local"
        return config, False


def _sender(agent: Agent) -> Callable[[EmailMessage], None]:
    if agent.email_ready:
        return outreach.smtp_sender(agent.config["outreach"])
    return outbox_sender(agent.run_id)


def outbox_sender(run_id: str) -> Callable[[EmailMessage], None]:
    """No Gmail settings: write each email to outbox/<run>/ instead of sending it."""
    folder = OUTBOX / run_id

    def send(message: EmailMessage) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        number = len(list(folder.glob("*.eml"))) + 1
        (folder / f"{number:03d}.eml").write_bytes(message.as_bytes())

    return send
