"""Durable research queue: claiming, recovery after a crash, retries and cancelling from another process."""

from datetime import timedelta

import pytest

from atlas.data.db import ResearchRun, session_scope, utcnow
from atlas.research import engine


def _run(run_id: str) -> ResearchRun:
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        s.expunge(run)
        return run


@pytest.fixture
def no_workers(monkeypatch):
    """Drive the queue by hand: no new worker threads, and ones other tests started stay idle."""
    monkeypatch.setattr(engine, "start_workers", lambda count=None: 0)
    monkeypatch.setattr(engine, "paused", True)


def test_only_queued_runs_are_claimed_and_each_by_one_worker(seeded, no_workers):
    direct = engine.create_run("What is happening with gold?", {"engine": "deterministic", "formats": []})
    queued = engine.submit("What is happening with gold?", {"engine": "deterministic", "formats": [], "cache": False})
    assert _run(direct).status == "created" and _run(queued).status == "queued"
    assert engine.claim_next("worker-a") == queued
    assert engine.claim_next("worker-b") is None  # already taken; the direct run is never picked up
    run = _run(queued)
    assert run.status == "running" and run.claimed_by == "worker-a" and run.attempts == 1 and run.heartbeat_at


def test_a_run_whose_server_died_is_retried_once_then_failed(seeded, no_workers):
    run_id = engine.submit("How is the U.S. dollar doing?", {"engine": "deterministic", "formats": [], "cache": False})
    assert engine.claim_next("crashed") == run_id
    with session_scope() as s:
        s.get(ResearchRun, run_id).cost_usd = 0.12  # Claude spend of the lost attempt
    later = utcnow() + engine.STALE_AFTER + timedelta(seconds=1)
    assert engine.recover_stale(later)["requeued"] >= 1  # other tests' abandoned runs may be recovered too
    run = _run(run_id)
    assert run.status == "queued" and run.claimed_by is None and run.config["earlier_attempts_cost_usd"] == 0.12
    assert "starting it again" in run.progress[-1]["message"]

    while engine.work_once("worker-b"):  # the second attempt finishes (oldest first, so earlier tests' runs go too)
        pass
    run = _run(run_id)
    assert run.status == "complete" and run.attempts == 2 and run.result["verification"]["passed"]

    lost = engine.submit("How is the euro doing?", {"engine": "deterministic", "formats": [], "cache": False})
    for _ in range(engine.MAX_ATTEMPTS):
        assert engine.claim_next("crashed") == lost
        later = utcnow() + engine.STALE_AFTER + timedelta(seconds=1)
        engine.recover_stale(later)
    run = _run(lost)
    assert run.status == "failed" and "stopped 2 times" in run.error


def test_recent_heartbeats_are_left_alone(seeded, no_workers):
    run_id = engine.submit("How is silver doing?", {"engine": "deterministic", "formats": [], "cache": False})
    engine.claim_next("alive")
    engine.recover_stale(utcnow() + timedelta(minutes=1))
    assert _run(run_id).status == "running" and _run(run_id).claimed_by == "alive"


def test_cancel_works_from_any_process(seeded, no_workers):
    waiting = engine.submit("How is copper doing?", {"engine": "deterministic", "formats": [], "cache": False})
    engine.cancel(waiting)
    assert _run(waiting).status == "cancelled" and engine.claim_next("w") is None

    running = engine.submit("How is wheat doing?", {"engine": "deterministic", "formats": [], "cache": False})
    assert engine.claim_next("w") == running
    with session_scope() as s:  # another replica asks to cancel: only the database flag reaches this process
        s.get(ResearchRun, running).cancel_requested_at = utcnow()
    assert running not in engine._cancelled
    with pytest.raises(RuntimeError, match="cancelled"):
        engine._progress(running, "next step")
