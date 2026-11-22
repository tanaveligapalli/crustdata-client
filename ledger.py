"""The cost ledger: one row for every outside call, cached or not, model calls included."""

from __future__ import annotations

import sqlite3

from agent_db import now_iso


def record(db: sqlite3.Connection, run_id: str, provider: str, endpoint: str, status: str,
           credits: float | None = None, cost_usd: float | None = None,
           request_hash: str | None = None, duration_ms: int | None = None) -> None:
    db.execute(
        """INSERT INTO ledger
           (run_id, provider, endpoint, status, credits, cost_usd, request_hash, duration_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, provider, endpoint, status, credits, cost_usd, request_hash, duration_ms, now_iso()),
    )
    db.commit()


def credits_spent(db: sqlite3.Connection, run_id: str) -> float:
    row = db.execute(
        "SELECT COALESCE(SUM(credits), 0) FROM ledger WHERE run_id = ?", (run_id,)
    ).fetchone()
    return row[0]
