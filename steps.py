"""Restart safety: finished steps are not run again, and side effects happen at most once."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from agent_db import now_iso


class ActionNeedsCheck(Exception):
    """An action started before a crash and we can't tell if it finished. A person must check."""


def run_step(db: sqlite3.Connection, run_id: str, name: str, work: Callable[[], Any]) -> Any:
    """Run `work()` once per run. After a restart, a finished step returns its saved output."""
    row = db.execute(
        "SELECT output_json FROM steps WHERE run_id = ? AND name = ?", (run_id, name)
    ).fetchone()
    if row:
        return json.loads(row["output_json"])

    output = work()
    db.execute(
        "INSERT INTO steps VALUES (?, ?, ?, ?)", (run_id, name, json.dumps(output), now_iso())
    )
    db.commit()
    return output


def do_once(db: sqlite3.Connection, run_id: str, action_key: str, kind: str,
            action: Callable[[], Any]) -> Any:
    """Perform a side effect (send an email, book a meeting, take a payment) at most once.

    The action is marked "started" before it runs. If the process dies before it is marked
    "done", we can't know whether it happened, so we stop and ask a person instead of repeating it.
    """
    row = db.execute(
        "SELECT status, result_json FROM actions WHERE action_key = ?", (action_key,)
    ).fetchone()
    if row and row["status"] == "done":
        return json.loads(row["result_json"])
    if row and row["status"] == "started":
        raise ActionNeedsCheck(f"{kind} {action_key} may or may not have happened")

    db.execute(
        "INSERT INTO actions VALUES (?, ?, ?, 'started', NULL, ?)", (action_key, run_id, kind, now_iso())
    )
    db.commit()

    result = action()

    db.execute(
        "UPDATE actions SET status = 'done', result_json = ?, updated_at = ? WHERE action_key = ?",
        (json.dumps(result), now_iso(), action_key),
    )
    db.commit()
    return result
