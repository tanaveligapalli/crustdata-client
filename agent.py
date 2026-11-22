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
import os
import sqlite3
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

import agent_db
import brief as brief_mod
import candidates as candidates_mod
import host_check
import instagram_sponsors
import outreach
import scoring
from crustdata_client import CrustdataClient
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
    return "run-" + hashlib.sha256(description.strip().encode("utf-8")).hexdigest()[:10]


def open_agent(description: str, offline: bool = False, credit_limit: float = 150) -> Agent:
    run_id = run_id_for(description)
    db = agent_db.connect()
    client = CrustdataClient(run_id=run_id, ledger_db=db, credit_limit=credit_limit, offline=offline)
    outreach_config, email_ready = _outreach_config()
    config = {"brief": brief_mod.load_config(), "candidates": candidates_mod.load_config(),
              "instagram": instagram_sponsors.load_config(), "scoring": scoring.load_config(),
              "outreach": outreach_config}
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
        return {"candidates": [asdict(c) for c in found], "failed_pools": failed}

    data = run_step(agent.db, agent.run_id, "candidates", work)
    found = [candidate_from_dict(c) for c in data["candidates"]]
    return scoring.rank(found, brief, agent.config["scoring"]), data["failed_pools"]


def find_contact(agent: Agent, candidate: Candidate) -> Contact | None:
    """Only for companies the host selected: each lookup costs credits."""
    def work() -> dict | None:
        contact = outreach.find_contact(agent.client, candidate, agent.config["outreach"])
        return asdict(contact) if contact else None

    data = run_step(agent.db, agent.run_id, f"contact:{candidate.company_id}", work)
    return contact_from_dict(data) if data else None


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
