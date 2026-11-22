"""The agent's own SQLite database: the cost ledger, finished steps and one-time actions.

(API responses are cached separately by crustdata_client.py.)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "agent.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    provider     TEXT NOT NULL,   -- crustdata, anthropic, ...
    endpoint     TEXT NOT NULL,
    status       TEXT NOT NULL,   -- ok, cached, error
    credits      REAL,            -- Crustdata credits charged (0 for a cache hit)
    cost_usd     REAL,            -- model calls
    request_hash TEXT,
    duration_ms  INTEGER,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    run_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    output_json TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS actions (
    action_key  TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- email, booking, payment
    status      TEXT NOT NULL,   -- started, done
    result_json TEXT,
    updated_at  TEXT NOT NULL
);
"""


def connect(path: str | Path = DEFAULT_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
