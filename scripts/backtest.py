"""Run the block 5 back-test on facts already in the evidence ledger. No API or model calls.

Usage:
    python scripts/backtest.py RUN_ID [RUN_ID ...] [--profile conference] [--db agent.sqlite3] [--out reports/backtest.md]

RUN_IDs are earlier runs of candidates.find_candidates with the post pools on (they save
the past-sponsor facts). More runs over more months of posts give more testable events.
The report says "cannot verify" when the data can't support a number (floors in config.yaml).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import backtest  # noqa: E402
import candidates  # noqa: E402
import scoring  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_ids", nargs="+")
    parser.add_argument("--profile", default="conference", choices=["conference", "concert"])
    parser.add_argument("--db", default=str(agent_db.DEFAULT_PATH))
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"No database at {args.db}: run candidates.find_candidates with the post pools first.")
        return 2
    db = agent_db.connect(args.db)
    facts = backtest.load_facts(db, args.run_ids)
    runs = db.execute("SELECT run_id, COUNT(*) FROM evidence WHERE subject LIKE 'company:%' "
                      "GROUP BY run_id ORDER BY MIN(saved_at)").fetchall()
    db.close()
    if not facts:
        print(f"No company facts saved for runs {', '.join(args.run_ids)}. Runs in this database:")
        for run_id, count in runs:
            print(f"  {run_id}  ({count} company facts)")
        return 2

    result = backtest.run(facts, args.profile, backtest.load_config(), candidates.load_config(),
                          scoring.load_config())
    text = backtest.report(result)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
