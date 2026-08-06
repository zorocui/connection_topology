from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

T = TypeVar("T")
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
DATABASE_UNAVAILABLE_MESSAGE = "数据库暂时不可用，请稍后重试"
TRANSACTION_CONFLICT_MESSAGE = "数据库事务冲突，请重试"


def postgres_sqlstate(exc: Exception) -> str | None:
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None)


class DatabaseUnavailable(RuntimeError):
    def __init__(self, operation_name: str) -> None:
        super().__init__(DATABASE_UNAVAILABLE_MESSAGE)
        self.operation_name = operation_name


class TransactionConflict(RuntimeError):
    def __init__(self, operation_name: str) -> None:
        super().__init__(TRANSACTION_CONFLICT_MESSAGE)
        self.operation_name = operation_name


def _rollback_safely(session: Session) -> None:
    try:
        session.rollback()
    except SQLAlchemyError:
        # Preserve the original classified failure without logging SQL or parameters.
        pass


class PostgresTransactionRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry_delays: tuple[float, ...] = (0.05, 0.15, 0.4),
        *,
        max_concurrent_transactions: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.retry_delays = tuple(retry_delays)
        capacity = max_concurrent_transactions or self._pool_capacity(session_factory)
        self._transaction_slots = (
            threading.BoundedSemaphore(capacity) if capacity is not None else None
        )
        self._admission_state = threading.local()

    @staticmethod
    def _pool_capacity(session_factory: sessionmaker[Session]) -> int | None:
        bind = getattr(session_factory, "kw", {}).get("bind")
        pool = getattr(bind, "pool", None)
        size = getattr(pool, "size", None)
        if not callable(size):
            return None
        pool_size = size()
        max_overflow = getattr(pool, "_max_overflow", 0)
        if not isinstance(pool_size, int) or not isinstance(max_overflow, int):
            return None
        return max(1, pool_size + max(0, max_overflow))

    @contextmanager
    def _admit_transaction(self) -> Iterator[None]:
        slots = self._transaction_slots
        if slots is None:
            yield
            return
        depth = getattr(self._admission_state, "depth", 0)
        if depth:
            self._admission_state.depth = depth + 1
            try:
                yield
            finally:
                self._admission_state.depth = depth
            return
        slots.acquire()
        self._admission_state.depth = 1
        try:
            yield
        finally:
            self._admission_state.depth = 0
            slots.release()

    def run(
        self,
        operation_name: str,
        operation: Callable[[Session], T],
    ) -> T:
        for attempt in range(len(self.retry_delays) + 1):
            session: Session | None = None
            try:
                with self._admit_transaction(), self.session_factory() as session:
                    result = operation(session)
                    session.commit()
                    return result
            except DBAPIError as exc:
                if session is not None:
                    _rollback_safely(session)
                sqlstate = postgres_sqlstate(exc)
                if sqlstate not in RETRYABLE_SQLSTATES:
                    if isinstance(exc, OperationalError):
                        raise DatabaseUnavailable(operation_name) from exc
                    raise
                if attempt == len(self.retry_delays):
                    raise TransactionConflict(operation_name) from exc
                logger.warning(
                    "PostgreSQL transaction retry operation=%s "
                    "sqlstate=%s attempt=%s/%s",
                    operation_name,
                    sqlstate,
                    attempt + 1,
                    len(self.retry_delays) + 1,
                )
            except TimeoutError as exc:
                if session is not None:
                    _rollback_safely(session)
                raise DatabaseUnavailable(operation_name) from exc

            time.sleep(self.retry_delays[attempt])

        raise AssertionError("事务重试循环未返回")

    @contextmanager
    def guard(self, operation_name: str) -> Iterator[None]:
        try:
            with self._admit_transaction():
                yield
        except DBAPIError as exc:
            if postgres_sqlstate(exc) in RETRYABLE_SQLSTATES:
                raise TransactionConflict(operation_name) from exc
            if isinstance(exc, OperationalError):
                raise DatabaseUnavailable(operation_name) from exc
            raise
        except TimeoutError as exc:
            raise DatabaseUnavailable(operation_name) from exc
