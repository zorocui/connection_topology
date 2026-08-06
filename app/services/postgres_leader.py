from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

SCHEDULER_LEADER_LOCK_KEY = 740_003


class PostgresLeaderElector:
    """Hold a PostgreSQL session advisory lock on one dedicated connection."""

    def __init__(
        self,
        engine: Engine,
        lock_key: int,
        on_acquired: Callable[[], None],
        on_lost: Callable[[], None],
        *,
        retry_seconds: float = 2.0,
    ) -> None:
        self.engine = engine
        self.lock_key = lock_key
        self.on_acquired = on_acquired
        self.on_lost = on_lost
        self.retry_seconds = retry_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._callback_lock = threading.Lock()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"postgres-leader-{self.lock_key}",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
        thread.join()
        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            connection = None
            acquired = False
            try:
                connection = self.engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                acquired = bool(
                    connection.scalar(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": self.lock_key},
                    )
                )
                if not acquired:
                    connection.close()
                    connection = None
                    self._stop_event.wait(self.retry_seconds)
                    continue
                with self._callback_lock:
                    self.on_acquired()
                while not self._stop_event.wait(self.retry_seconds):
                    connection.execute(text("SELECT 1"))
            except Exception as exc:  # noqa: BLE001 - reconnect after any lost session
                logger.warning(
                    "postgres leader connection lost lock_key=%s error_type=%s",
                    self.lock_key,
                    type(exc).__name__,
                )
            finally:
                if connection is not None:
                    if acquired:
                        try:
                            connection.execute(
                                text("SELECT pg_advisory_unlock(:lock_key)"),
                                {"lock_key": self.lock_key},
                            )
                        except Exception:  # noqa: BLE001 - connection may already be gone
                            logger.debug(
                                "postgres leader unlock skipped lock_key=%s",
                                self.lock_key,
                            )
                    connection.close()
                if acquired:
                    with self._callback_lock:
                        self.on_lost()
            if not self._stop_event.is_set():
                self._stop_event.wait(self.retry_seconds)
