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

from atlas.data.db import ReportSchedule, session_scope, utcnow

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
        "schedule_id": s.schedule_id, "name": s.name, "query": s.query, "config": s.config, "cadence": s.cadence,
        "status": s.status, "next_run_at": iso(s.next_run_at), "last_run_at": iso(s.last_run_at),
        "last_run_id": s.last_run_id, "created_at": iso(s.created_at),
    }


def create_schedule(name: str, query: str, cadence: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    with session_scope() as s:
        sched = ReportSchedule(name=name, query=query, cadence=cadence, config=config or {},
                               next_run_at=next_run(cadence, utcnow()))
        s.add(sched)
        s.flush()
        return _to_dict(sched)


def list_schedules() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(select(ReportSchedule).where(ReportSchedule.status != "deleted")
                         .order_by(ReportSchedule.created_at.desc())).scalars().all()
        return [_to_dict(r) for r in rows]


def delete_schedule(schedule_id: str) -> bool:
    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        if sched is None:
            return False
        sched.status = "deleted"
        return True


def run_schedule(schedule_id: str, *, background: bool = True) -> str:
    """Start a research run for a schedule now and advance its next run time."""
    from atlas.research import engine

    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        if sched is None or sched.status == "deleted":
            raise KeyError(schedule_id)
        query, config, cadence = sched.query, dict(sched.config or {}), sched.cadence
    config.setdefault("formats", ["html", "xlsx"])
    config["schedule_id"] = schedule_id
    run_id = engine.submit(query, config) if background else engine.create_run(query, config)
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
    with session_scope() as s:
        due = [r.schedule_id for r in s.execute(
            select(ReportSchedule).where(ReportSchedule.status == "active", ReportSchedule.next_run_at <= now)
        ).scalars()]
    started = []
    for sid in due:
        try:
            started.append(run_schedule(sid, background=background))
        except Exception:
            log.exception("scheduled report %s failed to start", sid)
    return started
