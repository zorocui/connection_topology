from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy
from threading import Lock


class TopologyCache:
    def __init__(
        self,
        ttl_seconds: float = 30,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._entries: dict[tuple, tuple[float, dict]] = {}
        self._lock = Lock()

    def get(self, key: tuple) -> dict | None:
        now = self.clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            return deepcopy(value)

    def put(self, key: tuple, value: dict) -> None:
        with self._lock:
            self._entries[key] = (
                self.clock() + self.ttl_seconds,
                deepcopy(value),
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
