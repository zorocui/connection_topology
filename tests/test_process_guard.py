import pytest

from app.services.process_guard import (
    SQLiteProcessAlreadyRunning,
    SQLiteProcessGuard,
    sqlite_database_path,
)


def test_sqlite_database_path_resolves_file_and_skips_non_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sqlite_database_path("sqlite:///./app.db") == (tmp_path / "app.db").resolve()
    assert sqlite_database_path("sqlite:///:memory:") is None
    assert sqlite_database_path("postgresql://db/app") is None


def test_second_guard_fails_until_first_releases(tmp_path):
    database_path = tmp_path / "guarded.db"
    first = SQLiteProcessGuard(database_path)
    second = SQLiteProcessGuard(database_path)
    first.acquire()
    try:
        with pytest.raises(SQLiteProcessAlreadyRunning, match="只支持一个应用进程"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_guards_for_different_databases_do_not_conflict(tmp_path):
    first = SQLiteProcessGuard(tmp_path / "first.db")
    second = SQLiteProcessGuard(tmp_path / "second.db")
    first.acquire()
    second.acquire()
    second.release()
    first.release()


def test_memory_database_guard_is_noop():
    guard = SQLiteProcessGuard(None)
    guard.acquire()
    guard.acquire()
    guard.release()
