import logging
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.models import SystemSetting
from app.services.sqlite_writes import (
    DatabaseBusy,
    SQLiteWriteCoordinator,
    is_transient_sqlite_write_error,
)


def locked_error(message="database is locked"):
    return OperationalError("UPDATE devices SET name=?", (), Exception(message))


@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_transient_detection_accepts_only_sqlite_lock_messages(message):
    assert is_transient_sqlite_write_error(locked_error(message)) is True
    assert is_transient_sqlite_write_error(ValueError(message)) is False
    assert is_transient_sqlite_write_error(locked_error("disk I/O error")) is False


def test_write_retries_with_fresh_sessions_and_commits(app, caplog):
    coordinator = SQLiteWriteCoordinator(
        app.state.session_factory,
        retry_delays=(0.0, 0.0),
    )
    session_ids = []
    attempts = 0

    def operation(session):
        nonlocal attempts
        attempts += 1
        session_ids.append(id(session))
        if attempts < 3:
            raise locked_error()
        session.merge(SystemSetting(id=1, history_retention_days=9))
        return "saved"

    with caplog.at_level(logging.WARNING, logger="app.services.sqlite_writes"):
        assert coordinator.write("test_write", operation) == "saved"

    assert attempts == 3
    assert len(set(session_ids)) == 3
    assert "operation=test_write" in caplog.text
    assert "UPDATE system_settings" not in caplog.text
    with app.state.session_factory() as session:
        assert session.execute(
            text("SELECT history_retention_days FROM system_settings WHERE id=1")
        ).scalar_one() == 9


def test_write_exhaustion_raises_database_busy(app):
    coordinator = SQLiteWriteCoordinator(
        app.state.session_factory,
        retry_delays=(0.0, 0.0),
    )
    with pytest.raises(DatabaseBusy) as captured:
        coordinator.write(
            "persist_scan",
            lambda session: (_ for _ in ()).throw(locked_error()),
        )
    assert captured.value.operation_name == "persist_scan"
    assert str(captured.value) == "数据库繁忙，扫描结果未能保存，请重试"


def test_non_transient_error_is_not_retried(app):
    coordinator = SQLiteWriteCoordinator(app.state.session_factory, (0.0, 0.0))
    attempts = 0

    def operation(session):
        nonlocal attempts
        attempts += 1
        raise ValueError("programming error")

    with pytest.raises(ValueError, match="programming error"):
        coordinator.write("broken", operation)
    assert attempts == 1


def test_write_and_write_once_share_one_reentrant_lock(app):
    coordinator = SQLiteWriteCoordinator(app.state.session_factory, (0.0,))
    entered = []
    barrier = threading.Barrier(2)

    def first():
        with coordinator.write_once("first"):
            entered.append("first")
            barrier.wait()
            coordinator.write("nested", lambda session: entered.append("nested"))

    thread = threading.Thread(target=first)
    thread.start()
    barrier.wait()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert entered == ["first", "nested"]


def test_write_once_converts_transient_error_to_database_busy(app):
    coordinator = SQLiteWriteCoordinator(app.state.session_factory, (0.0,))
    with (
        pytest.raises(DatabaseBusy) as captured,
        coordinator.write_once("api_update_device"),
    ):
        raise locked_error("database is busy")
    assert captured.value.operation_name == "api_update_device"
