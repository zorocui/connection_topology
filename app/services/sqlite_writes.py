from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
T = TypeVar("T")
DATABASE_BUSY_MESSAGE = "数据库繁忙，扫描结果未能保存，请重试"


def is_transient_sqlite_write_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database" in message and ("locked" in message or "busy" in message)


class DatabaseBusy(RuntimeError):
    def __init__(self, operation_name: str) -> None:
        super().__init__(DATABASE_BUSY_MESSAGE)
        self.operation_name = operation_name


class SQLiteWriteCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry_delays: tuple[float, ...],
        *,
        enabled: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.retry_delays = retry_delays
        self.enabled = enabled
        self._lock = threading.RLock()

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        if self.enabled:
            with self._lock:
                yield
        else:
            yield

    @contextmanager
    def write_once(self, operation_name: str) -> Iterator[None]:
        with self._serialized():
            try:
                yield
            except Exception as exc:
                if self.enabled and is_transient_sqlite_write_error(exc):
                    raise DatabaseBusy(operation_name) from exc
                raise

    def write(
        self,
        operation_name: str,
        operation: Callable[[Session], T],
    ) -> T:
        attempts = len(self.retry_delays) + 1
        with self._serialized():
            for attempt in range(attempts):
                with self.session_factory() as session:
                    try:
                        result = operation(session)
                        session.commit()
                        return result
                    except Exception as exc:
                        session.rollback()
                        if not self.enabled or not is_transient_sqlite_write_error(exc):
                            raise
                        if attempt == len(self.retry_delays):
                            raise DatabaseBusy(operation_name) from exc
                        logger.warning(
                            "SQLite 写入繁忙 operation=%s attempt=%s/%s",
                            operation_name,
                            attempt + 1,
                            attempts,
                        )
                time.sleep(self.retry_delays[attempt])
        raise AssertionError("SQLite 写入重试循环未返回")
