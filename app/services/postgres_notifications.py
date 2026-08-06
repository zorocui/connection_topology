from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable

import psycopg
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TOPOLOGY_CHANNEL = "topology_changed"
_CHANNEL_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def notify_topology_changed(session: Session) -> None:
    session.execute(
        text("SELECT pg_notify(:channel, '')"),
        {"channel": TOPOLOGY_CHANNEL},
    )


class PostgresNotificationListener:
    def __init__(
        self,
        database_url: str,
        channel: str,
        callback: Callable[[], None],
        *,
        retry_seconds: float = 1.0,
    ) -> None:
        if not _CHANNEL_PATTERN.fullmatch(channel):
            raise ValueError("invalid PostgreSQL notification channel")
        self.connection_uri = make_url(database_url).set(
            drivername="postgresql"
        ).render_as_string(hide_password=False)
        self.channel = channel
        self.callback = callback
        self.retry_seconds = retry_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"postgres-listen-{self.channel}",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
        thread.join(timeout=3)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with psycopg.connect(self.connection_uri, autocommit=True) as connection:
                    connection.execute(
                        sql.SQL("LISTEN {}").format(sql.Identifier(self.channel))
                    )
                    while not self._stop_event.is_set():
                        for _notification in connection.notifies(
                            timeout=1.0, stop_after=1
                        ):
                            self.callback()
            except psycopg.Error as exc:
                logger.warning(
                    "postgres notification listener reconnecting channel=%s error_type=%s",
                    self.channel,
                    type(exc).__name__,
                )
                self._stop_event.wait(self.retry_seconds)
            except Exception as exc:  # noqa: BLE001 - callback must not kill listener
                logger.error(
                    "postgres notification callback failed channel=%s error_type=%s",
                    self.channel,
                    type(exc).__name__,
                )
                self._stop_event.wait(self.retry_seconds)
