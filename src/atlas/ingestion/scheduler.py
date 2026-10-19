"""Lightweight refresh scheduler.

Polls for datasets whose refresh interval has elapsed and ingests them. This keeps the MVP
dependency-free; the same `ingest()` entry point can be wrapped as Dagster assets later.
"""

from __future__ import annotations

import logging
import time

from atlas.ingestion.pipeline import due_datasets, ingest

log = logging.getLogger("atlas.scheduler")


def run_once() -> int:
    """Refresh due datasets, then start any scheduled reports that are due (so they use fresh data)."""
    from atlas import calendar, documents
    from atlas.schedules import run_due

    cal = calendar.refresh_if_due()
    if cal:
        log.info("calendar refreshed: %s", cal)
    docs = documents.refresh_if_due()
    if docs:
        log.info("document feeds refreshed: %s", docs)
    due = due_datasets()
    if due:
        log.info("refreshing %d due dataset(s)", len(due))
        ingest(due)
    started = run_due()
    if started:
        log.info("started %d scheduled report(s)", len(started))
    return len(due)


def run_forever(poll_seconds: int = 300) -> None:
    while True:
        try:
            run_once()
        except Exception:
            log.exception("scheduler iteration failed")
        time.sleep(poll_seconds)
