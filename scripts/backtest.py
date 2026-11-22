"""Run the block 5 back-test.

Usage:
    python scripts/backtest.py RUN_ID [RUN_ID ...]          # facts already in the ledger: no API or model calls
    python scripts/backtest.py --collect 2025-11:2026-09 --posts-per-month 5 --credit-limit 50
    options: [--profile conference] [--db agent.sqlite3] [--out reports/backtest.md]

RUN_IDs are earlier runs of candidates.find_candidates with the post pools on (they save
the past-sponsor facts). --collect first reads `--posts-per-month` LinkedIn posts per month
with block 3's post pool (0.5 credits and one model call per post, plus name lookups) into
the run "backtest-<profile>", then tests on it. It needs CRUSTDATA_API_KEY and
ANTHROPIC_API_KEY, stops at --credit-limit, and a rerun skips months already collected.

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
import instagram_sponsors  # noqa: E402
import scoring  # noqa: E402
from crustdata_client import BudgetExceeded, CrustdataClient, SourceUnavailable  # noqa: E402
from src import extract  # noqa: E402


def collect(args, db) -> str | None:
    """Collect into the run "backtest-<profile>". Returns the run ID, or None if stopped."""
    run_id = f"backtest-{args.profile}"
    first, last = args.collect.split(":")
    ig_config = instagram_sponsors.load_config()
    client = CrustdataClient(run_id=run_id, ledger_db=db, credit_limit=args.credit_limit)
    try:
        saved = backtest.collect(backtest.month_starts(first, last), args.posts_per_month, args.profile,
                                 client, db, run_id, candidates.load_config(), ig_config,
                                 extract.anthropic_caller(ig_config))
    except (BudgetExceeded, SourceUnavailable) as e:
        print(f"Collection stopped: {e}. Rerun to continue; finished months are kept.")
        return None
    finally:
        client.close()
    print("Sponsor facts saved per month:", saved)
    for provider, calls, credits, usd in db.execute(
            "SELECT provider, COUNT(*), COALESCE(SUM(credits), 0), COALESCE(SUM(cost_usd), 0) "
            "FROM ledger WHERE run_id = ? GROUP BY provider", (run_id,)):
        print(f"  spent so far in {run_id}: {provider} {calls} calls, {credits:g} credits, ${usd:.2f}")
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_ids", nargs="*")
    parser.add_argument("--profile", default="conference", choices=["conference", "concert"])
    parser.add_argument("--db", default=str(agent_db.DEFAULT_PATH))
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument("--collect", metavar="YYYY-MM:YYYY-MM", help="collect posts for these months first")
    parser.add_argument("--posts-per-month", type=int, default=5)
    parser.add_argument("--credit-limit", type=float, default=50.0)
    args = parser.parse_args()

    db = agent_db.connect(args.db)
    run_ids = list(args.run_ids)
    if args.collect:
        run_id = collect(args, db)
        if run_id is None:
            return 1
        run_ids.append(run_id)
    if not run_ids:
        parser.error("give RUN_IDs, --collect, or both")

    facts = backtest.load_facts(db, run_ids)
    runs = db.execute("SELECT run_id, COUNT(*) FROM evidence WHERE subject LIKE 'company:%' "
                      "GROUP BY run_id ORDER BY MIN(saved_at)").fetchall()
    db.close()
    if not facts:
        print(f"No company facts saved for runs {', '.join(run_ids)}. Runs in this database:")
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
