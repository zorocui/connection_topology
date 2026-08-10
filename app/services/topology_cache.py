from __future__ import annotations

import time
import threading
from collections.abc import Callable
from copy import deepcopy
from threading import Lock


class _Inflight:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.error: BaseException | None = None


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
        self._inflight: dict[tuple, _Inflight] = {}
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

    def get_or_compute(self, key: tuple, factory: Callable[[], dict]) -> dict:
        """Return the cached value or compute it once for concurrent callers.

        While one caller computes, others waiting on the same key block and
        then receive the stored result (or the same exception), so a slow
        topology build runs once per key instead of once per request.
        """
        while True:
            cached = self.get(key)
            if cached is not None:
                return cached
            with self._lock:
                inflight = self._inflight.get(key)
                if inflight is None:
                    inflight = _Inflight()
                    self._inflight[key] = inflight
                    owner = True
                else:
                    owner = False
            if not owner:
                inflight.event.wait()
                if inflight.error is not None:
                    raise inflight.error
                continue
            try:
                value = factory()
                self.put(key, value)
                return value
            except Exception as exc:
                inflight.error = exc
                raise
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                inflight.event.set()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
