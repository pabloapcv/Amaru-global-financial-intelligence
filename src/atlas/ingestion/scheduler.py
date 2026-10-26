"""Lightweight refresh scheduler.

Polls for datasets whose refresh interval has elapsed and ingests them, refreshes the calendar
and document feeds, then starts scheduled reports. Only one scheduler works at a time: each
iteration first takes (or renews) a lease row in the database, so running the scheduler in the API
process and in its own container, or scaling it, never runs reports or alerts twice. The lease
expires if its holder dies, and another process takes over.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from atlas.data.db import SchedulerLease, session_scope, utcnow
from atlas.ingestion.pipeline import due_datasets, ingest

log = logging.getLogger("atlas.scheduler")

LEASE = "scheduler"
LEASE_TTL = timedelta(minutes=30)  # longer than the slowest iteration (e.g. a Comtrade quota wait)
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def acquire(holder: str = HOLDER, name: str = LEASE, ttl: timedelta = LEASE_TTL) -> bool:
    """Take or renew the lease; False when another live process holds it."""
    now = utcnow()
    with session_scope() as s:
        res = s.execute(update(SchedulerLease)
                        .where(SchedulerLease.name == name,
                               or_(SchedulerLease.holder == holder, SchedulerLease.expires_at < now))
                        .values(holder=holder, expires_at=now + ttl, heartbeat_at=now))
        if res.rowcount:
            return True
    try:
        with session_scope() as s:
            if s.get(SchedulerLease, name) is not None:
                return False
            s.add(SchedulerLease(name=name, holder=holder, expires_at=now + ttl, heartbeat_at=now))
        return True
    except IntegrityError:  # another process created it first
        return False


def release(holder: str = HOLDER, name: str = LEASE) -> None:
    with session_scope() as s:
        s.execute(update(SchedulerLease).where(SchedulerLease.name == name, SchedulerLease.holder == holder)
                  .values(expires_at=utcnow()))


def _record(result: dict[str, Any], holder: str) -> None:
    with session_scope() as s:
        s.execute(update(SchedulerLease).where(SchedulerLease.name == LEASE, SchedulerLease.holder == holder)
                  .values(last_run_at=utcnow(), heartbeat_at=utcnow(), last_result=result))


def status() -> dict[str, Any]:
    """Who holds the lease and when the scheduler last finished an iteration."""
    with session_scope() as s:
        lease = s.get(SchedulerLease, LEASE)
        if lease is None:
            return {"active": False, "last_run_at": None, "holder": None}
        now = utcnow()
        iso = lambda d: d.isoformat(timespec="seconds") + "Z" if d else None  # noqa: E731
        return {"active": lease.expires_at > now, "holder": lease.holder, "last_run_at": iso(lease.last_run_at),
                "heartbeat_at": iso(lease.heartbeat_at), "last_result": lease.last_result,
                "minutes_since_last_run": round((now - lease.last_run_at).total_seconds() / 60, 1) if lease.last_run_at else None}


def run_once(holder: str = HOLDER) -> int:
    """Refresh due datasets, then start any scheduled reports that are due (so they use fresh data).
    Returns the number of datasets refreshed, or -1 when another scheduler holds the lease."""
    from atlas import calendar, documents
    from atlas.schedules import run_due

    if not acquire(holder):
        log.info("another scheduler holds the lease; skipping this iteration")
        return -1
    cal = calendar.refresh_if_due()
    if cal:
        log.info("calendar refreshed: %s", cal)
    docs = documents.refresh_if_due()
    if docs:
        log.info("document feeds refreshed: %s", docs)
        if any(v.get("new") for v in docs.values()):
            from atlas.agents import evaluate_release_agents

            evaluate_release_agents()
    due = due_datasets()
    if due:
        log.info("refreshing %d due dataset(s)", len(due))
        ingest(due)
    started = run_due()
    if started:
        log.info("started %d scheduled report(s)", len(started))
    from atlas import digest

    digests = digest.send_due()
    if digests:
        log.info("sent %d weekly digest(s)", digests)
    from atlas import webhooks

    retried = webhooks.deliver_due()
    from atlas import newsletters

    newsletters.resume_stalled()
    _record({"datasets_refreshed": len(due), "reports_started": len(started), "digests_sent": digests,
             "webhook_retries": retried}, holder)
    return len(due)


def run_forever(poll_seconds: int = 300) -> None:
    try:
        while True:
            try:
                run_once()
            except Exception:
                log.exception("scheduler iteration failed")
            time.sleep(poll_seconds)
    finally:
        release()
