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
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd in ("init-db", "ingest", "schedule", "research"):
        init_db()
        with session_scope() as s:
            sync_registry(s)
    if args.cmd == "ingest":
        from atlas.ingestion.pipeline import ingest

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
