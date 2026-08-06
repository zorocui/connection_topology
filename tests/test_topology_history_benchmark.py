from scripts.benchmark_topology_history import run_benchmark


def test_history_benchmark_smoke(migrated_engine):
    result = run_benchmark(
        rows=5_000,
        max_seconds=10,
        engine=migrated_engine,
    )

    assert result["raw_rows"] == 5_000
    assert result["service_groups"] > 0
    assert result["within_target"] is True
    assert result["scoped_groups"] > 0
    assert result["scoped_groups"] <= result["service_groups"]
    assert result["scoped_within_target"] is True
