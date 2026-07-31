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
