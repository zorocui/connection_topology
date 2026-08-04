from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.engine import make_url

PROCESS_GUARD_MESSAGE = (
    "SQLite 模式只支持一个应用进程；请移除 uvicorn --workers，"
    "远程扫描并发由 SCAN_MAX_WORKERS 提供"
)


def sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        return None
    return Path(url.database).resolve()


class SQLiteProcessAlreadyRunning(RuntimeError):
    pass


class SQLiteProcessGuard:
    def __init__(self, database_path: Path | None) -> None:
        self.database_path = database_path
        self.lock_path = (
            database_path.with_name(f"{database_path.name}.app.lock")
            if database_path is not None
            else None
        )
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self.lock_path is None or self._handle is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise SQLiteProcessAlreadyRunning(PROCESS_GUARD_MESSAGE) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
