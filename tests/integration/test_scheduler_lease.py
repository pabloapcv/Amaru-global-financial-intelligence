"""The scheduler lease: a one-off pass hands it back so a running scheduler isn't kept waiting."""

from atlas.ingestion import scheduler


def test_schedule_once_releases_the_lease(isolated_env, monkeypatch):
    from atlas import cli

    def fake_pass(holder: str = scheduler.HOLDER) -> int:
        assert scheduler.acquire(holder)
        return 3

    monkeypatch.setattr(scheduler, "run_once", fake_pass)
    cli.main(["schedule", "--once"])
    assert scheduler.acquire("api-process")  # free at once, not after the 30-minute lease
    scheduler.release("api-process")


def test_a_held_lease_still_blocks_another_scheduler(isolated_env):
    assert scheduler.acquire("first")
    assert not scheduler.acquire("second")
    scheduler.release("first")
    assert scheduler.acquire("second")
    scheduler.release("second")
