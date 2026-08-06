import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, TimeoutError

from app.services.database_transactions import (
    DATABASE_UNAVAILABLE_MESSAGE,
    TRANSACTION_CONFLICT_MESSAGE,
    DatabaseUnavailable,
    PostgresTransactionRunner,
    TransactionConflict,
)


class DriverError(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


def db_error(sqlstate, *, parameters=None):
    return OperationalError(
        "UPDATE devices SET encrypted_password=%(password)s",
        parameters or {"password": "RawCredential!"},
        DriverError(sqlstate),
    )


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
def test_runner_retries_only_transaction_conflicts_with_fresh_sessions(app, sqlstate):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0, 0.0))
    sessions = []

    def operation(session):
        sessions.append(session)
        if len(sessions) < 3:
            raise db_error(sqlstate)
        return "saved"

    assert runner.run("save", operation) == "saved"
    assert len(sessions) == 3
    assert len({id(session) for session in sessions}) == 3


def test_runner_does_not_retry_unique_violation(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0, 0.0))
    attempts = 0

    def operation(session):
        nonlocal attempts
        attempts += 1
        raise IntegrityError(
            "INSERT INTO devices (encrypted_password) VALUES (%(password)s)",
            {"password": "RawCredential!"},
            DriverError("23505"),
        )

    with pytest.raises(IntegrityError):
        runner.run("duplicate", operation)
    assert attempts == 1


def test_retry_exhaustion_raises_safe_transaction_conflict(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0,))

    with pytest.raises(TransactionConflict) as caught:
        runner.run("persist_scan", lambda session: (_ for _ in ()).throw(db_error("40001")))

    assert str(caught.value) == TRANSACTION_CONFLICT_MESSAGE
    assert caught.value.operation_name == "persist_scan"
    assert "UPDATE devices" not in str(caught.value)
    assert "RawCredential!" not in str(caught.value)


def test_connectivity_failure_maps_to_safe_unavailable_error(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0,))

    with pytest.raises(DatabaseUnavailable) as caught:
        runner.run("load", lambda session: (_ for _ in ()).throw(db_error(None)))

    assert str(caught.value) == DATABASE_UNAVAILABLE_MESSAGE
    assert caught.value.operation_name == "load"
    assert "UPDATE devices" not in str(caught.value)
    assert "RawCredential!" not in str(caught.value)


def test_pool_timeout_maps_to_safe_unavailable_error(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0,))

    with pytest.raises(DatabaseUnavailable) as caught:
        runner.run(
            "load",
            lambda session: (_ for _ in ()).throw(TimeoutError("pool exposed secret")),
        )

    assert str(caught.value) == DATABASE_UNAVAILABLE_MESSAGE
    assert "pool exposed secret" not in str(caught.value)


def test_runner_limits_database_callbacks_to_configured_capacity(app):
    runner = PostgresTransactionRunner(
        app.state.session_factory,
        (0.0,),
        max_concurrent_transactions=2,
    )
    lock = threading.Lock()
    active = 0
    maximum = 0

    def operation(session):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            session.execute(text("SELECT 1"))
            time.sleep(0.02)
        finally:
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(runner.run, "limited", operation) for _ in range(10)]
        for future in futures:
            future.result()

    assert maximum == 2


def test_guard_limits_caller_owned_transactions_to_configured_capacity(app):
    runner = PostgresTransactionRunner(
        app.state.session_factory,
        max_concurrent_transactions=2,
    )
    lock = threading.Lock()
    active = 0
    maximum = 0

    def guarded_operation():
        nonlocal active, maximum
        with runner.guard("guarded"):
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.02)
            finally:
                with lock:
                    active -= 1

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(guarded_operation) for _ in range(10)]
        for future in futures:
            future.result()

    assert maximum == 2


def test_guard_releases_transaction_slot_after_exception(app):
    runner = PostgresTransactionRunner(
        app.state.session_factory,
        max_concurrent_transactions=1,
    )

    with pytest.raises(ValueError, match="failed"), runner.guard("failing_guard"):
        raise ValueError("failed")

    entered = threading.Event()

    def enter_guard():
        with runner.guard("after_failure"):
            entered.set()

    thread = threading.Thread(target=enter_guard, daemon=True)
    thread.start()
    assert entered.wait(timeout=1)
    thread.join(timeout=1)
    assert thread.is_alive() is False


def test_guard_rejects_run_that_would_checkout_a_second_connection(app):
    runner = PostgresTransactionRunner(
        app.state.session_factory,
        max_concurrent_transactions=1,
    )

    with runner.guard("request_owned"), pytest.raises(
        RuntimeError,
        match="independent database session",
    ):
        runner.run(
            "nested_write",
            lambda session: session.scalar(text("SELECT 1")),
        )


@pytest.mark.parametrize(
    "factory_error",
    [db_error(None), TimeoutError("pool exposed secret")],
)
def test_session_factory_failures_map_to_safe_unavailable_error(
    factory_error,
):
    def failing_session_factory():
        raise factory_error

    runner = PostgresTransactionRunner(failing_session_factory, (0.0,))

    with pytest.raises(DatabaseUnavailable) as caught:
        runner.run("connect", lambda session: "unused")

    assert str(caught.value) == DATABASE_UNAVAILABLE_MESSAGE
    assert "UPDATE devices" not in str(caught.value)
    assert "RawCredential!" not in str(caught.value)
    assert "pool exposed secret" not in str(caught.value)


def test_guard_maps_conflict_without_replaying_caller_owned_session(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0, 0.0))
    entries = 0

    with pytest.raises(TransactionConflict) as caught, runner.guard("api_update_device"):
        entries += 1
        raise db_error("40P01")

    assert entries == 1
    assert caught.value.operation_name == "api_update_device"


def test_guard_maps_operational_and_pool_failures_to_unavailable(app):
    runner = PostgresTransactionRunner(app.state.session_factory)

    with pytest.raises(DatabaseUnavailable), runner.guard("api_load"):
        raise db_error(None)

    with pytest.raises(DatabaseUnavailable), runner.guard("api_load"):
        raise TimeoutError("pool exposed secret")


def test_retry_warning_does_not_log_sql_parameters_or_credentials(app, caplog):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0,))
    transaction_logger = logging.getLogger("app.services.database_transactions")
    transaction_logger.disabled = False
    attempts = 0

    def operation(session):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise db_error("40001")
        return "saved"

    with caplog.at_level(logging.WARNING, logger="app.services.database_transactions"):
        assert runner.run("persist_scan", operation) == "saved"

    assert "operation=persist_scan" in caplog.text
    assert "sqlstate=40001" in caplog.text
    assert "UPDATE devices" not in caplog.text
    assert "RawCredential!" not in caplog.text
