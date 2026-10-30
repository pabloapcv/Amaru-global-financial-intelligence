"""Research job orchestration: plan → investigate → write → verify → publish (spec §8.1, §12)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from atlas.data.db import LOCAL_WORKSPACE, ResearchEvidence, ResearchRun, session_scope, utcnow
from atlas.instruments import extract_entities, get_dataset
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


def create_run(query: str, config: dict[str, Any] | None = None, user_id: str = "local",
               workspace_id: str = LOCAL_WORKSPACE) -> str:
    with session_scope() as s:
        run = ResearchRun(query=query, config=config or {}, user_id=user_id, workspace_id=workspace_id,
                          status="queued", progress=[], cache_key=cache_key(query, config or {}),
                          parent_run_id=(config or {}).get("parent_run_id"))
        s.add(run)
        s.flush()
        return run.run_id


def submit(query: str, config: dict[str, Any] | None = None, user_id: str = "local",
           workspace_id: str = LOCAL_WORKSPACE) -> str:
    run_id = create_run(query, config, user_id, workspace_id)
    _executor.submit(execute_run, run_id)
    return run_id


def cancel(run_id: str) -> None:
    with _lock:
        _cancelled.add(run_id)


def _save_usage(run_id: str, researchers: list) -> None:
    if researchers:
        usage = researchers[0].usage
        _set(run_id, llm_usage=usage, cost_usd=usage["cost_usd"])


# ---------------------------------------------------------------- result cache

CACHE_CONFIG_KEYS = ("depth", "engine", "template", "lookback_years", "horizon_months", "assets", "formats", "documents_query",
                     "parent_run_id", "language", "scenario_universe", "custom_scenarios", "scenario_portfolio")


def cache_key(query: str, config: dict[str, Any]) -> str:
    import hashlib
    import json
    import re

    norm = re.sub(r"\s+", " ", query.strip().lower())
    cfg = {k: config.get(k) for k in CACHE_CONFIG_KEYS}
    return hashlib.sha256(json.dumps([norm, cfg], sort_keys=True, default=str).encode()).hexdigest()


def find_cached(query: str, config: dict[str, Any], workspace_id: str) -> str | None:
    """A completed run with the same question and settings, newer than the TTL and than any data change."""
    import os
    from datetime import timedelta

    from sqlalchemy import func, select

    from atlas.data.db import DatasetState

    ttl = float(os.getenv("ATLAS_RESEARCH_CACHE_TTL_HOURS", "6"))
    if ttl <= 0 or config.get("cache") is False:
        return None
    key = cache_key(query, config)
    with session_scope() as s:
        last_change = s.execute(select(func.max(DatasetState.last_changed_at))).scalar()
        q = (select(ResearchRun).where(
            ResearchRun.workspace_id == workspace_id, ResearchRun.cache_key == key, ResearchRun.status == "complete",
            ResearchRun.cached_from.is_(None), ResearchRun.finished_at >= utcnow() - timedelta(hours=ttl))
            .order_by(ResearchRun.finished_at.desc()).limit(1))
        hit = s.execute(q).scalars().first()
        if hit is None or (last_change is not None and hit.finished_at < last_change):
            return None
        return hit.run_id


def submit_or_cached(query: str, config: dict[str, Any], user_id: str, workspace_id: str,
                     before_submit: Callable[[], None] | None = None) -> tuple[str, bool]:
    """Serve an identical recent answer instantly (as a new, auditable run record) or start a new run.

    `before_submit` (e.g. a quota check) runs only when new research is actually started.
    """
    original = find_cached(query, config, workspace_id)
    if original is None:
        if before_submit:
            before_submit()
        return submit(query, config, user_id, workspace_id), False
    with session_scope() as s:
        src = s.get(ResearchRun, original)
        assert src is not None
        copy = ResearchRun(
            query=query, config=config, user_id=user_id, workspace_id=workspace_id, status="complete",
            engine=src.engine, model_version=src.model_version, plan=src.plan,
            result={**(src.result or {}), "cached": True, "cached_from": original},
            progress=[{"at": utcnow().isoformat(timespec="seconds") + "Z",
                       "message": f"Served from cache: identical question answered at {src.finished_at.isoformat(timespec='seconds')}Z with unchanged data"}],
            cache_key=src.cache_key, cached_from=original, cost_usd=0.0, finished_at=utcnow(),
        )
        s.add(copy)
        s.flush()
        return copy.run_id, True


def _evidence_dicts(ctx: T.ToolContext) -> list[dict[str, Any]]:
    return [
        {"label": e.label, "tool": e.tool, "args": e.args, "result": e.result, "citation": e.citation,
         "sources": e.sources, "chart": e.chart}
        for e in ctx.evidence
    ]


MAX_THREAD = 5


def prior_context(parent_run_id: str, workspace_id: str) -> dict[str, Any] | None:
    """What a follow-up needs from its thread: earlier questions (oldest first), the parent's answer
    with its evidence labels removed (they belong to the earlier run), and the parent's instruments."""
    import re

    parent = get_run(parent_run_id, workspace_id)
    if parent is None or parent["status"] != "complete" or not parent.get("result"):
        return None
    questions, cur = [parent["query"]], parent
    while cur and cur.get("parent_run_id") and len(questions) < MAX_THREAD:
        cur = get_run(cur["parent_run_id"], workspace_id)
        if cur:
            questions.insert(0, cur["query"])
    answer = re.sub(r"\s*\[E\d+(?:\s*,\s*E\d+)*\]", "", parent["result"].get("narrative_markdown", ""))
    plan = parent.get("plan") or {}
    instruments = [e for e in plan.get("entities", []) if (ds := get_dataset(e)) and ds.kind in ("market", "macro")]
    return {"run_id": parent_run_id, "query": parent["query"], "questions": questions, "answer": answer[:4000],
            "instruments": instruments[:5], "template": (parent.get("config") or {}).get("template")}


def execute_run(run_id: str) -> dict[str, Any]:
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        assert run is not None
        query, config, workspace_id = run.query, dict(run.config or {}), run.workspace_id
        run.status = "running"
    researchers: list[llm.LLMResearcher] = []  # kept so token usage is recorded even if the run fails
    try:
        depth = config.get("depth", "standard")
        engine_pref = config.get("engine", "auto")
        _progress(run_id, "Parsing intent and resolving instruments")
        if config.get("engine_note"):
            _progress(run_id, config["engine_note"])
        prior = prior_context(config["parent_run_id"], workspace_id) if config.get("parent_run_id") else None
        plan_query = query
        if prior and not [e for e in extract_entities(query) if (ds := get_dataset(e)) and ds.kind in ("market", "macro")]:
            # An elliptical follow-up ("What about over ten years?") continues the earlier subject and analysis;
            # its own text comes first so its settings (here the lookback) win.
            plan_query = f"{query} {prior['query']}"
        plan = build_plan(
            plan_query, depth=depth, horizon_months=config.get("horizon_months"),
            lookback_years=config.get("lookback_years"), scope=config.get("assets"),
        )
        plan["query"] = query
        if config.get("scenario_universe") or config.get("custom_scenarios") or config.get("scenario_portfolio"):
            plan["scenario_inputs"] = {"universe": config.get("scenario_universe"), "custom_scenarios": config.get("custom_scenarios"),
                                       "portfolio_id": config.get("scenario_portfolio")}
        if prior:
            plan["follow_up_to"] = {"run_id": prior["run_id"], "query": prior["query"]}
        from atlas.research import templates

        plan = templates.apply(plan, config.get("template"))
        if config.get("documents_query"):  # e.g. an agent fired by an official release: cite the release
            plan["steps"] = [{"tool": "search_documents", "args": {"query": config["documents_query"], "days": 14}}] + [
                st for st in plan["steps"] if st["tool"] != "search_documents"]
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
        if use_llm:
            from atlas import auth

            spent, budget = auth.llm_spend_this_month(workspace_id), auth.llm_budget(workspace_id)
            if budget is not None and spent >= budget:
                msg = f"monthly Claude budget reached (${spent:.2f} of ${budget:.2f})"
                if engine_pref == "llm":
                    raise RuntimeError(msg)
                _progress(run_id, f"{msg}; using deterministic engine")
                use_llm = False
        ctx = T.ToolContext(workspace_id=workspace_id)
        narrative, engine, model = "", "deterministic", None
        transcript = None
        if use_llm:
            try:
                researcher = llm.LLMResearcher()
                researchers.append(researcher)
                _progress(run_id, f"Investigating with {researcher.model}")
                narrative, transcript = researcher.run(query, plan, ctx, lambda m: _progress(run_id, m), depth, prior,
                                                       config.get("language"))
                engine, model = "llm", researcher.model
            except Exception as exc:
                # An explicit LLM request fails loudly; only "auto" degrades to the deterministic engine.
                if "cancelled" in str(exc) or engine_pref == "llm":
                    raise
                log.warning("LLM research failed, falling back to deterministic: %s", exc)
                _progress(run_id, f"LLM unavailable ({type(exc).__name__}: {str(exc)[:200]}); using deterministic engine")
                ctx = T.ToolContext(workspace_id=workspace_id)
        if engine == "deterministic":
            for step in plan["steps"]:
                _progress(run_id, f"Running {step['tool']}")
                T.execute(ctx, step["tool"], step["args"])
            narrative = writer.compose(query, plan, _evidence_dicts(ctx))

        evidence = _evidence_dicts(ctx)
        _progress(run_id, "Verifying numerical claims and citations")
        asked = query + (" " + " ".join(prior["questions"]) if prior else "")
        from atlas import i18n

        language = i18n.lang(config.get("language"))
        decimal_comma = engine == "llm" and language in i18n.DECIMAL_COMMA  # the deterministic writer writes English
        check = verify.verify(narrative, evidence, asked, decimal_comma)
        if engine == "llm" and not check["passed"] and transcript is not None:
            _progress(run_id, f"{len(check['unverified_numbers'])} untraceable figure(s); requesting revision")
            try:
                revised = researcher.revise(transcript, check)  # type: ignore[possibly-undefined]
                recheck = verify.verify(revised, evidence, asked, decimal_comma)
                if len(recheck["unverified_numbers"]) <= len(check["unverified_numbers"]):
                    narrative, check = revised, recheck
            except Exception as exc:
                log.warning("revision failed: %s", exc)

        if engine == "deterministic" and i18n.labels(language)["deterministic_note"]:
            lines = narrative.splitlines()
            at = 1 if lines and lines[0].startswith("# ") else 0
            narrative = "\n".join(lines[:at] + ["", i18n.labels(language)["deterministic_note"]] + lines[at:])
        if prior:  # say what this answers, under the title (after verification: it quotes a question, not a claim)
            lines = narrative.splitlines()
            at = 1 if lines and lines[0].startswith("# ") else 0
            narrative = "\n".join(lines[:at] + ["", f"*Follow-up to: {prior['query']}*"] + lines[at:])
        _save_usage(run_id, researchers)
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
            "llm_usage": researchers[0].usage if researchers else None,
            **({"engine_note": config["engine_note"]} if config.get("engine_note") else {}),
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
        _emit_finished(run_id)
        return result
    except Exception as exc:
        _save_usage(run_id, researchers)
        log.exception("research run %s failed", run_id)
        status = "cancelled" if "cancelled" in str(exc) else "failed"
        _set(run_id, status=status, error=f"{type(exc).__name__}: {exc}", finished_at=utcnow())
        _emit_finished(run_id)
        return {"error": str(exc)}


def _emit_finished(run_id: str) -> None:
    from atlas import webhooks

    try:
        webhooks.research_event(run_id)
    except Exception:
        log.exception("webhook event for run %s failed", run_id)
    from atlas import newsletters

    try:
        newsletters.from_scheduled_run(run_id)
    except Exception:
        log.exception("newsletter issue for run %s failed", run_id)
    from atlas import scenarios

    try:
        scenarios.notify_changes(run_id)
    except Exception:
        log.exception("scenario change notice for run %s failed", run_id)


def _summary(r: ResearchRun | None) -> dict[str, Any] | None:
    return None if r is None else {"run_id": r.run_id, "query": r.query, "status": r.status,
                                   "title": (r.result or {}).get("title")}


def get_run(run_id: str, workspace_id: str | None = None) -> dict[str, Any] | None:
    """Fetch a run. With `workspace_id`, runs from other workspaces are invisible (treated as missing)."""
    from sqlalchemy import select

    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None or run.status == "deleted" or (workspace_id is not None and run.workspace_id != workspace_id):
            return None
        return {
            "run_id": run.run_id, "workspace_id": run.workspace_id, "query": run.query, "config": run.config, "status": run.status,
            "cost_usd": run.cost_usd, "cached_from": run.cached_from,
            "engine": run.engine, "model_version": run.model_version, "plan": run.plan, "result": run.result,
            "progress": run.progress, "error": run.error, "parent_run_id": run.parent_run_id,
            "parent": _summary(s.get(ResearchRun, run.parent_run_id)) if run.parent_run_id else None,
            "follow_ups": [_summary(r) for r in s.execute(select(ResearchRun).where(
                ResearchRun.parent_run_id == run.run_id, ResearchRun.workspace_id == run.workspace_id)
                .order_by(ResearchRun.created_at)).scalars()],
            "created_at": run.created_at.isoformat(timespec="seconds") + "Z",
            "finished_at": run.finished_at.isoformat(timespec="seconds") + "Z" if run.finished_at else None,
        }


def delete_run(run_id: str, workspace_id: str, user_id: str, role: str) -> bool:
    """Delete a run's content: evidence, reports (rows and files) and their share links are removed;
    the run record keeps only what accounting needs (time, engine, Claude cost) so budgets and quotas
    cannot be reset by deleting. Follow-ups, cached copies, schedules and agent events are unlinked.
    The run's creator or a workspace owner/admin may delete; returns False if not visible."""
    import shutil
    from pathlib import Path

    from sqlalchemy import delete, select, update

    from atlas.config import settings
    from atlas.data.db import AgentEvent, Report, ReportSchedule, ReportShare

    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None or run.workspace_id != workspace_id or run.status == "deleted":
            return False
        if run.user_id != user_id and role not in ("owner", "admin"):
            raise PermissionError("only the run's author or a workspace owner/admin can delete it")
        if run.status in ("queued", "running"):
            raise ValueError("cancel the run before deleting it")
        report_ids = list(s.execute(select(Report.report_id).where(Report.run_id == run_id)).scalars())
        if report_ids:
            s.execute(delete(ReportShare).where(ReportShare.report_id.in_(report_ids)))
            s.execute(delete(Report).where(Report.report_id.in_(report_ids)))
        s.execute(delete(ResearchEvidence).where(ResearchEvidence.run_id == run_id))
        from atlas.data.db import ResearchFeedback

        s.execute(delete(ResearchFeedback).where(ResearchFeedback.run_id == run_id))  # it quotes the question
        s.execute(update(ResearchRun).where(ResearchRun.parent_run_id == run_id).values(parent_run_id=None))
        s.execute(update(ResearchRun).where(ResearchRun.cached_from == run_id).values(cached_from=None))
        s.execute(update(ReportSchedule).where(ReportSchedule.last_run_id == run_id).values(last_run_id=None))
        s.execute(update(AgentEvent).where(AgentEvent.run_id == run_id).values(run_id=None))
        run.status, run.query, run.plan, run.result, run.progress, run.error = "deleted", "[deleted]", None, None, [], None
        run.config, run.cache_key, run.parent_run_id = {}, None, None
    for rid in report_ids:
        shutil.rmtree(Path(settings.object_store) / "reports" / rid, ignore_errors=True)
    from atlas.auth import audit

    audit("research.deleted", user_id, workspace_id, run_id=run_id, reports=len(report_ids))
    return True


def list_runs(limit: int = 50, workspace_id: str | None = None, q_text: str | None = None) -> list[dict[str, Any]]:
    from sqlalchemy import select

    with session_scope() as s:
        q = select(ResearchRun).where(ResearchRun.status != "deleted")
        if workspace_id is not None:
            q = q.where(ResearchRun.workspace_id == workspace_id)
        if q_text:
            q = q.where(ResearchRun.query.ilike(f"%{q_text.strip()}%"))
        runs = s.execute(q.order_by(ResearchRun.created_at.desc()).limit(limit)).scalars().all()
        return [
            {"run_id": r.run_id, "query": r.query, "status": r.status, "engine": r.engine, "user_id": r.user_id,
             "title": (r.result or {}).get("title"), "report_id": (r.result or {}).get("report_id"),
             "created_at": r.created_at.isoformat(timespec="seconds") + "Z"}
            for r in runs
        ]

