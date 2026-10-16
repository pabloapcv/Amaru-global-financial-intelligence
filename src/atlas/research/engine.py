"""Research job orchestration: plan → investigate → write → verify → publish (spec §8.1, §12)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from atlas.data.db import ResearchEvidence, ResearchRun, session_scope, utcnow
from atlas.research import llm, tools as T, verify, writer
from atlas.research.planner import build_plan, validate_plan

log = logging.getLogger("atlas.research")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="research")
_cancelled: set[str] = set()
_lock = threading.Lock()


def _progress(run_id: str, message: str) -> None:
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None:
            return
        run.progress = [*(run.progress or []), {"at": utcnow().isoformat(timespec="seconds") + "Z", "message": message}]
    if run_id in _cancelled:
        raise RuntimeError("cancelled by user")


def _set(run_id: str, **fields: Any) -> None:
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        assert run is not None
        for k, v in fields.items():
            setattr(run, k, v)


def create_run(query: str, config: dict[str, Any] | None = None, user_id: str = "local") -> str:
    with session_scope() as s:
        run = ResearchRun(query=query, config=config or {}, user_id=user_id, status="queued", progress=[])
        s.add(run)
        s.flush()
        return run.run_id


def submit(query: str, config: dict[str, Any] | None = None, user_id: str = "local") -> str:
    run_id = create_run(query, config, user_id)
    _executor.submit(execute_run, run_id)
    return run_id


def cancel(run_id: str) -> None:
    with _lock:
        _cancelled.add(run_id)


def _evidence_dicts(ctx: T.ToolContext) -> list[dict[str, Any]]:
    return [
        {"label": e.label, "tool": e.tool, "args": e.args, "result": e.result, "citation": e.citation,
         "sources": e.sources, "chart": e.chart}
        for e in ctx.evidence
    ]


def execute_run(run_id: str) -> dict[str, Any]:
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        assert run is not None
        query, config = run.query, dict(run.config or {})
        run.status = "running"
    try:
        depth = config.get("depth", "standard")
        engine_pref = config.get("engine", "auto")
        _progress(run_id, "Parsing intent and resolving instruments")
        plan = build_plan(
            query, depth=depth, horizon_months=config.get("horizon_months"),
            lookback_years=config.get("lookback_years"), scope=config.get("assets"),
        )
        problems = validate_plan(plan)
        if problems:
            raise ValueError("invalid research plan: " + "; ".join(problems))
        _set(run_id, plan=plan)
        _progress(run_id, f"Plan: {len(plan['steps'])} steps over {len(plan['entities'])} datasets")

        if engine_pref == "llm" and not llm.available():
            raise RuntimeError(
                "engine=llm requested but no Anthropic credentials were found. Set ANTHROPIC_API_KEY in the "
                "environment or in .env at the project root (one line: ANTHROPIC_API_KEY=sk-ant-...)."
            )
        use_llm = engine_pref == "llm" or (engine_pref == "auto" and llm.available())
        ctx = T.ToolContext()
        narrative, engine, model = "", "deterministic", None
        transcript = None
        if use_llm:
            try:
                researcher = llm.LLMResearcher()
                _progress(run_id, f"Investigating with {researcher.model}")
                narrative, transcript = researcher.run(query, plan, ctx, lambda m: _progress(run_id, m), depth)
                engine, model = "llm", researcher.model
            except Exception as exc:
                # An explicit LLM request fails loudly; only "auto" degrades to the deterministic engine.
                if "cancelled" in str(exc) or engine_pref == "llm":
                    raise
                log.warning("LLM research failed, falling back to deterministic: %s", exc)
                _progress(run_id, f"LLM unavailable ({type(exc).__name__}: {str(exc)[:200]}); using deterministic engine")
                ctx = T.ToolContext()
        if engine == "deterministic":
            for step in plan["steps"]:
                _progress(run_id, f"Running {step['tool']}")
                T.execute(ctx, step["tool"], step["args"])
            narrative = writer.compose(query, plan, _evidence_dicts(ctx))

        evidence = _evidence_dicts(ctx)
        _progress(run_id, "Verifying numerical claims and citations")
        check = verify.verify(narrative, evidence, query)
        if engine == "llm" and not check["passed"] and transcript is not None:
            _progress(run_id, f"{len(check['unverified_numbers'])} untraceable figure(s); requesting revision")
            try:
                revised = researcher.revise(transcript, check)  # type: ignore[possibly-undefined]
                recheck = verify.verify(revised, evidence, query)
                if len(recheck["unverified_numbers"]) <= len(check["unverified_numbers"]):
                    narrative, check = revised, recheck
            except Exception as exc:
                log.warning("revision failed: %s", exc)

        title = next((ln[2:].strip() for ln in narrative.splitlines() if ln.startswith("# ")), query)
        sources = {}
        for e in evidence:
            for src in e["sources"]:
                sources[src["dataset_id"]] = src
        result = {
            "title": title,
            "narrative_markdown": narrative,
            "engine": engine,
            "model": model,
            "verification": check,
            "evidence": [{k: v for k, v in e.items() if k != "chart"} for e in evidence],
            "charts": [{"evidence": e["label"], **e["chart"]} for e in evidence if e.get("chart")],
            "sources": list(sources.values()),
            "tool_log": ctx.log,
        }
        with session_scope() as s:
            for e in evidence:
                s.add(ResearchEvidence(
                    evidence_id=f"{run_id}:{e['label']}", run_id=run_id, label=e["label"], tool=e["tool"],
                    args=e["args"], result=e["result"], citation=e["citation"],
                ))
        _set(run_id, result=result, engine=engine, model_version=model)

        if config.get("formats", ["html"]):
            _progress(run_id, "Rendering report")
            from atlas.reporting import build_report

            report = build_report(run_id, formats=config.get("formats", ["html", "xlsx"]), template=config.get("template", "research_report"))
            result["report_id"] = report["report_id"]
            _set(run_id, result=result)
        _progress(run_id, "Complete")
        _set(run_id, status="complete", finished_at=utcnow())
        return result
    except Exception as exc:
        log.exception("research run %s failed", run_id)
        status = "cancelled" if "cancelled" in str(exc) else "failed"
        _set(run_id, status=status, error=f"{type(exc).__name__}: {exc}", finished_at=utcnow())
        return {"error": str(exc)}


def get_run(run_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None:
            return None
        return {
            "run_id": run.run_id, "query": run.query, "config": run.config, "status": run.status,
            "engine": run.engine, "model_version": run.model_version, "plan": run.plan, "result": run.result,
            "progress": run.progress, "error": run.error,
            "created_at": run.created_at.isoformat(timespec="seconds") + "Z",
            "finished_at": run.finished_at.isoformat(timespec="seconds") + "Z" if run.finished_at else None,
        }


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    from sqlalchemy import select

    with session_scope() as s:
        runs = s.execute(select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(limit)).scalars().all()
        return [
            {"run_id": r.run_id, "query": r.query, "status": r.status, "engine": r.engine,
             "title": (r.result or {}).get("title"), "report_id": (r.result or {}).get("report_id"),
             "created_at": r.created_at.isoformat(timespec="seconds") + "Z"}
            for r in runs
        ]

