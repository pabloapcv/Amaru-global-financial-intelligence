"""Scheduled research reports (spec FR-010): saved questions re-run on a cadence.

Each run is a normal research job, so every scheduled report is versioned, verified and
auditable. Schedules are evaluated by the same scheduler loop that refreshes data, after
the data refresh, so reports always use the freshest stored observations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from atlas.data.db import LOCAL_WORKSPACE, ReportSchedule, session_scope, utcnow

log = logging.getLogger("atlas.schedules")

CADENCES = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}
DEFAULT_HOUR_UTC = 11  # after U.S. morning data releases


def next_run(cadence: str, after: datetime) -> datetime:
    if cadence not in CADENCES:
        raise ValueError(f"cadence must be one of {sorted(CADENCES)}")
    base = after.replace(hour=DEFAULT_HOUR_UTC, minute=0, second=0, microsecond=0)
    if base <= after:
        base += timedelta(days=1)
    if cadence == "weekly":
        base += timedelta(days=(0 - base.weekday()) % 7)  # Mondays
    elif cadence == "monthly":
        while base.day != 1:
            base += timedelta(days=1)
    return base


def _to_dict(s: ReportSchedule) -> dict[str, Any]:
    iso = lambda d: d.isoformat(timespec="seconds") + "Z" if d else None  # noqa: E731
    return {
        "schedule_id": s.schedule_id, "workspace_id": s.workspace_id, "name": s.name, "query": s.query, "config": s.config, "cadence": s.cadence,
        "status": s.status, "next_run_at": iso(s.next_run_at), "last_run_at": iso(s.last_run_at),
        "last_run_id": s.last_run_id, "created_at": iso(s.created_at),
    }


def create_schedule(name: str, query: str, cadence: str, config: dict[str, Any] | None = None,
                    workspace_id: str = LOCAL_WORKSPACE, user_id: str = "local") -> dict[str, Any]:
    with session_scope() as s:
        sched = ReportSchedule(name=name, query=query, cadence=cadence, config=config or {},
                               workspace_id=workspace_id, user_id=user_id, next_run_at=next_run(cadence, utcnow()))
        s.add(sched)
        s.flush()
        return _to_dict(sched)


def list_schedules(workspace_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(ReportSchedule).where(ReportSchedule.status != "deleted")
        if workspace_id is not None:
            q = q.where(ReportSchedule.workspace_id == workspace_id)
        rows = s.execute(q.order_by(ReportSchedule.created_at.desc())).scalars().all()
        return [_to_dict(r) for r in rows]


def set_newsletter_mode(schedule_id: str, workspace_id: str, mode: str) -> dict[str, Any] | None:
    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        if sched is None or sched.status == "deleted" or sched.workspace_id != workspace_id:
            return None
        sched.config = {**(sched.config or {}), "newsletter": mode}
        return _to_dict(sched)


def delete_schedule(schedule_id: str, workspace_id: str | None = None) -> bool:
    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        if sched is None or (workspace_id is not None and sched.workspace_id != workspace_id):
            return False
        sched.status = "deleted"
        return True


def run_schedule(schedule_id: str, *, background: bool = True, workspace_id: str | None = None) -> str:
    """Start a research run for a schedule now and advance its next run time."""
    from atlas.research import engine

    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        if sched is None or sched.status == "deleted" or (workspace_id is not None and sched.workspace_id != workspace_id):
            raise KeyError(schedule_id)
        query, config, cadence = sched.query, dict(sched.config or {}), sched.cadence
        ws, uid = sched.workspace_id, sched.user_id
    config.setdefault("formats", ["html", "xlsx"])
    config["schedule_id"] = schedule_id
    if config.get("newsletter") in ("draft", "send") and not config.get("language"):
        from atlas import newsletters

        language = newsletters.language_for(ws)  # an issue in Spanish needs research written in Spanish
        if language:
            config["language"] = language
    run_id = engine.submit(query, config, uid, ws) if background else engine.create_run(query, config, uid, ws)
    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        assert sched is not None
        sched.last_run_at, sched.last_run_id = utcnow(), run_id
        sched.next_run_at = next_run(cadence, utcnow())
    if not background:
        engine.execute_run(run_id)
    return run_id


def run_due(now: datetime | None = None, *, background: bool = True) -> list[str]:
    now = now or utcnow()
    from atlas.data.db import suspended_workspace_ids

    suspended = suspended_workspace_ids()
    with session_scope() as s:
        due = [r.schedule_id for r in s.execute(
            select(ReportSchedule).where(ReportSchedule.status == "active", ReportSchedule.next_run_at <= now)
        ).scalars() if r.workspace_id not in suspended]
    started = []
    for sid in due:
        try:
            started.append(run_schedule(sid, background=background))
        except Exception:
            log.exception("scheduled report %s failed to start", sid)
    return started
