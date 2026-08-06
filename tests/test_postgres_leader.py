import threading
import time

from app.services.postgres_leader import PostgresLeaderElector


def _wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_only_one_candidate_holds_scheduler_lock_and_second_takes_over(migrated_engine):
    events = []
    event_lock = threading.Lock()

    def record(value):
        with event_lock:
            events.append(value)

    first = PostgresLeaderElector(
        migrated_engine,
        740_003,
        lambda: record("first-acquired"),
        lambda: record("first-lost"),
        retry_seconds=0.05,
    )
    second = PostgresLeaderElector(
        migrated_engine,
        740_003,
        lambda: record("second-acquired"),
        lambda: record("second-lost"),
        retry_seconds=0.05,
    )
    first.start()
    _wait(lambda: "first-acquired" in events)
    second.start()
    time.sleep(0.15)
    assert [event for event in events if event.endswith("acquired")] == ["first-acquired"]
    first.shutdown()
    _wait(lambda: "second-acquired" in events)
    second.shutdown()
    assert events.count("first-lost") == 1
    assert events.count("second-lost") == 1


def test_start_and_shutdown_are_idempotent(migrated_engine):
    events = []
    elector = PostgresLeaderElector(
        migrated_engine,
        740_103,
        lambda: events.append("acquired"),
        lambda: events.append("lost"),
        retry_seconds=0.05,
    )
    elector.start()
    elector.start()
    _wait(lambda: events == ["acquired"])
    elector.shutdown()
    elector.shutdown()
    assert events == ["acquired", "lost"]
