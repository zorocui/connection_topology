import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.services.topology_cache import TopologyCache


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_topology_cache_isolated_copy_ttl_and_clear():
    clock = FakeClock()
    cache = TopologyCache(ttl_seconds=30, clock=clock)
    value = {"nodes": [{"data": {"id": "cluster-1"}}]}

    cache.put(("cluster", 1, "1d"), value)
    cached = cache.get(("cluster", 1, "1d"))
    assert cached == value
    cached["nodes"].clear()
    assert cache.get(("cluster", 1, "1d")) == value

    clock.advance(31)
    assert cache.get(("cluster", 1, "1d")) is None

    cache.put(("device", 1, "1d"), value)
    cache.clear()
    assert cache.get(("device", 1, "1d")) is None


def test_get_or_compute_computes_once_for_concurrent_callers():
    cache = TopologyCache(ttl_seconds=30)
    calls = []

    def factory():
        calls.append(1)
        time.sleep(0.05)
        return {"nodes": []}

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(
                lambda _: cache.get_or_compute(("cluster", 1, "7d"), factory),
                range(5),
            )
        )

    assert len(calls) == 1
    assert results == [{"nodes": []}] * 5


def test_get_or_compute_waiters_receive_factory_error():
    cache = TopologyCache(ttl_seconds=30)
    calls = []
    gate = threading.Event()
    errors = []

    def factory():
        calls.append(1)
        gate.wait(5)
        raise RuntimeError("boom")

    def call():
        try:
            cache.get_or_compute(("cluster", 1, "7d"), factory)
        except RuntimeError as exc:
            errors.append(str(exc))

    owner = threading.Thread(target=call)
    owner.start()
    while not calls:
        time.sleep(0.005)
    waiter = threading.Thread(target=call)
    waiter.start()
    time.sleep(0.05)
    gate.set()
    owner.join(5)
    waiter.join(5)

    assert sorted(errors) == ["boom", "boom"]
    assert len(calls) == 1

    value = cache.get_or_compute(("cluster", 1, "7d"), lambda: {"ok": True})
    assert value == {"ok": True}
    assert len(calls) == 1


def test_get_or_compute_returns_cached_value_without_compute():
    cache = TopologyCache(ttl_seconds=30)
    cache.put(("device", 1, "1d"), {"cached": True})

    def factory():
        raise AssertionError("must not compute")

    assert cache.get_or_compute(("device", 1, "1d"), factory) == {
        "cached": True
    }
