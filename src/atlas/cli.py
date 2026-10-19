"""ATLAS command line: `atlas init-db | ingest | status | schedule | serve | research | llm-check`."""

from __future__ import annotations

import argparse
import json
import logging

from atlas.data.db import init_db, session_scope
from atlas.instruments import sync_registry


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create tables and load the instrument registry")
    ing = sub.add_parser("ingest", help="run the ingestion pipeline")
    ing.add_argument("dataset_ids", nargs="*", help="defaults to every registered dataset")
    ing.add_argument("--backfill", action="store_true", help="full history instead of incremental")
    ing.add_argument("--calendar", action="store_true", help="refresh the economic calendar only")
    sub.add_parser("status", help="print dataset freshness")
    sch = sub.add_parser("schedule", help="run the refresh scheduler")
    sch.add_argument("--once", action="store_true")
    srv = sub.add_parser("serve", help="run the API server")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8010)
    rs = sub.add_parser("research", help="run a research question and write a report")
    rs.add_argument("query")
    rs.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard")
    rs.add_argument("--engine", choices=["auto", "llm", "deterministic"], default="auto")
    sub.add_parser("llm-check", help="verify Anthropic credentials with a minimal API request")
    sub.add_parser("workspaces", help="list workspaces, plans, members and this month's Claude spend")
    bm = sub.add_parser("benchmark", help="measure the spec's acceptance targets (§16) on this deployment's data")
    bm.add_argument("--json", action="store_true", help="print the full result as JSON")
    sp = sub.add_parser("set-plan", help="change a workspace's plan")
    sp.add_argument("workspace_id")
    sp.add_argument("plan")
    cu = sub.add_parser("create-user", help="create a user account (prompts for the password)")
    cu.add_argument("email")
    cu.add_argument("--name", default="")
    cu.add_argument("--workspace", help="join an existing workspace id (e.g. 'local' to own pre-existing data)")
    cu.add_argument("--plan", default="explorer", help="plan for a new workspace")
    cu.add_argument("--password-stdin", action="store_true", help="read the password from stdin (for scripts)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd in ("init-db", "ingest", "schedule", "research", "create-user", "workspaces", "set-plan", "benchmark"):
        init_db()
        with session_scope() as s:
            sync_registry(s)
    if args.cmd == "ingest":
        from atlas.ingestion.pipeline import ingest

        if args.calendar:
            from atlas import calendar

            print(json.dumps(calendar.refresh(), indent=2))
            return
        for r in ingest(args.dataset_ids or None, backfill=args.backfill):
            print(f"{r.dataset_id:24} {r.status:10} fetched={r.rows_fetched:6} inserted={r.rows_inserted:6} "
                  f"revised={r.rows_revised:4} {r.error or ''}")
    elif args.cmd == "status":
        from atlas.data.access import data_status

        print(json.dumps(data_status(), indent=2))
    elif args.cmd == "schedule":
        from atlas.ingestion.scheduler import run_forever, run_once

        print(f"refreshed {run_once()} dataset(s)") if args.once else run_forever()
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("atlas.api.main:app", host=args.host, port=args.port)
    elif args.cmd == "create-user":
        import getpass
        import sys

        from atlas import auth

        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Password (min 10 characters): ")
            if getpass.getpass("Repeat password: ") != password:
                raise SystemExit("passwords do not match")
        try:
            uid, ws = auth.create_user(args.email, password, args.name, workspace_id=args.workspace, plan=args.plan,
                                       verified=True)  # created by an operator
        except auth.ValidationError as exc:
            raise SystemExit(str(exc))
        print(f"created user {args.email} in workspace {ws}")
    elif args.cmd == "benchmark":
        from atlas.benchmark import run as run_benchmark

        logging.getLogger().setLevel(logging.WARNING)
        res = run_benchmark()
        if args.json:
            print(json.dumps(res, indent=2, default=str))
        else:
            for name, r in res.items():
                if not isinstance(r, dict):
                    continue
                detail = {k: v for k, v in r.items() if k not in ("target", "pass", "failures")}
                print(f"{'PASS' if r['pass'] else 'FAIL'}  {name:22} {r['target']:48} {detail}")
            print("ALL TARGETS MET" if res["all_pass"] else "SOME TARGETS MISSED")
        raise SystemExit(0 if res["all_pass"] else 1)
    elif args.cmd == "workspaces":
        from atlas import auth

        for w in auth.list_workspaces():
            print(f"{w['workspace_id']:32}  {w['plan']:12}  ${w['claude_spend_month_usd']:>7.2f}  {w['name']}  "
                  f"[{', '.join(w['members']) or 'no members'}]")
    elif args.cmd == "set-plan":
        from atlas import auth

        try:
            auth.set_plan(args.workspace_id, args.plan)
        except auth.ValidationError as exc:
            raise SystemExit(str(exc))
        print(f"workspace {args.workspace_id} is now on the {args.plan} plan")
    elif args.cmd == "llm-check":
        import os

        import anthropic

        from atlas.config import ROOT, settings
        from atlas.research import llm

        print(f".env at {ROOT / '.env'}: {'found' if (ROOT / '.env').exists() else 'NOT FOUND'}")
        key = os.getenv("ANTHROPIC_API_KEY", "")
        print(f"ANTHROPIC_API_KEY: {'set' if key else 'not set'}" + (f" (starts with sk-ant-: {key.startswith('sk-ant-')})" if key else ""))
        if not llm.available():
            raise SystemExit("No Anthropic credentials found; research will use the deterministic engine.")
        try:
            resp = anthropic.Anthropic().messages.create(
                model=settings.llm_model, max_tokens=64, output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            )
        except anthropic.AuthenticationError:
            raise SystemExit("Authentication failed: the API key was rejected.")
        except anthropic.APIStatusError as exc:
            raise SystemExit(f"API error {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError:
            raise SystemExit("Could not reach the Anthropic API (network).")
        print(f"OK: {settings.llm_model} responded (stop_reason={resp.stop_reason}, request_id={resp._request_id})")
    elif args.cmd == "research":
        from atlas.reporting import get_report
        from atlas.research.engine import create_run, execute_run

        run_id = create_run(args.query, {"depth": args.depth, "engine": args.engine, "formats": ["html", "xlsx"]})
        result = execute_run(run_id)
        if "error" in result:
            raise SystemExit(f"research failed: {result['error']}")
        print(result["narrative_markdown"])
        v = result["verification"]
        print(f"\n[engine={result['engine']} verified {v['numbers_verified']}/{v['numbers_checked']} numbers]")
        if args.engine != "deterministic" and result["engine"] != "llm":
            from atlas.research.engine import get_run

            notes = [p["message"] for p in get_run(run_id)["progress"] if "LLM unavailable" in p["message"]]
            print("WARNING: Claude was not used. " + (notes[0] if notes else "No Anthropic credentials found (run `atlas llm-check`)."))
        report = get_report(result["report_id"])
        if report:
            for fmt, path in report["paths"].items():
                print(f"{fmt}: {path}")


if __name__ == "__main__":
    main()
