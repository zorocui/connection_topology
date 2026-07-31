from scripts.benchmark_topology_history import run_benchmark


def test_history_benchmark_smoke(tmp_path):
    result = run_benchmark(
        rows=5_000,
        max_seconds=10,
        database_path=tmp_path / "benchmark.db",
    )

    assert result["raw_rows"] == 5_000
    assert result["service_groups"] > 0
    assert result["within_target"] is True
