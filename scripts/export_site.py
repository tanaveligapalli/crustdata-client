"""Build a read-only copy of the web screen for static hosting (Netlify):

    python scripts/export_site.py            # writes site/
    npx netlify-cli deploy --dir site --prod  # or drag site/ onto app.netlify.com/drop

Why a copy: Netlify serves files and short JavaScript/Go functions, while a search runs
Python for minutes and keeps its state in SQLite. So this writes what the replay server
(`web/server.py --offline`) would answer for each demo event that already ran on this
machine, as JSON files beside the page, and the page reads them. Nothing in the copy can
spend credits or send email: there is no server behind it.

Pitches and the pipeline are left out: they name real people (the contacts we found).

Live instead of read-only: `python scripts/export_site.py --backend https://<tunnel>` writes
only the page plus a Netlify proxy rule, so every /api call goes to a running
`web/server.py` (e.g. this laptop through `cloudflared tunnel --url http://127.0.0.1:8000`).
Then the Netlify page can search, draft and send, and spends credits: share it with care.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import agent  # noqa: E402
import agent_db  # noqa: E402
import server  # noqa: E402

OUT = ROOT / "site"
# Netlify rewrites (status 200): the page keeps asking /api/..., Netlify answers with the file.
REDIRECTS = """/api/config  /api/config.json  200
/api/runs    /api/runs.json    200
/api/runs/*  /api/runs/:splat.json  200
"""


def export(out: Path = OUT, db_path: Path = agent_db.DEFAULT_PATH) -> list[str]:
    """Write the site; returns the names of the events included."""
    db = agent_db.connect(db_path)
    scoring_cfg = server.App(offline=True, db_path=db_path).scoring_cfg
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(server.STATIC, out)
    (out / "api" / "runs").mkdir(parents=True)

    examples, runs = [], []
    for example in server.examples():
        run_id = agent.run_id_for(example["text"])
        if server.step_output(db, run_id, "candidates") is None:
            print(f"skipped {example['name']}: never ran on this machine (run it once on the live server)")
            continue
        view = server.run_view(db, run_id, scoring_cfg, debug=False)
        # They name real people (contacts, sponsors' replies and their addresses): not for a public page.
        for key in ("pitches", "pipeline", "people", "held_replies", "replies"):
            view[key] = []
        write(out / "api" / "runs" / f"{run_id}.json", view)
        examples.append({**example, "run_id": run_id})
        runs.append({"run_id": run_id, "created_at": None, "event_name": view["brief"]["event_name"]})
    db.close()

    write(out / "api" / "config.json", {"static": True, "offline": True, "debug_available": False,
                                        "credit_limit": 0, "email": "none", "examples": examples})
    write(out / "api" / "runs.json", runs)
    (out / "_redirects").write_text(REDIRECTS, encoding="utf-8")
    return [e["name"] for e in examples]


def export_live(backend: str, out: Path = OUT) -> None:
    """The page only; Netlify proxies /api/* to the live server at `backend`."""
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(server.STATIC, out)
    (out / "_redirects").write_text(f"/api/*  {backend.rstrip('/')}/api/:splat  200\n", encoding="utf-8")


def write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build site/ for Netlify.")
    parser.add_argument("--backend", help="a live web/server.py URL to proxy /api/* to (instead of a read-only copy)")
    args = parser.parse_args()
    if args.backend:
        export_live(args.backend)
        print(f"site/ ready: the page, with /api/* proxied to {args.backend}")
    else:
        names = export()
        print(f"site/ ready with {len(names)} events: {', '.join(names)}")
