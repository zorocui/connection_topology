import pytest

from app import preflight
from app.preflight import PostgreSQLLimits, run_preflight


def test_preflight_calculates_total_connection_budget(app, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "load_postgresql_limits",
        lambda engine: PostgreSQLLimits(server_version="15.18", max_connections=100),
    )
    report = run_preflight(app.state.settings, workers=8)
    assert report.requested_connections == 8 * (2 + 0 + 2)
    assert report.available_connections == 90


def test_preflight_rejects_excessive_connection_budget(app, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "load_postgresql_limits",
        lambda engine: PostgreSQLLimits(server_version="15.18", max_connections=40),
    )
    with pytest.raises(RuntimeError, match="连接数预算"):
        run_preflight(app.state.settings, workers=8)


def test_preflight_rejects_non_postgresql_15(app, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "load_postgresql_limits",
        lambda engine: PostgreSQLLimits(server_version="16.4", max_connections=100),
    )
    with pytest.raises(RuntimeError, match="PostgreSQL 15"):
        run_preflight(app.state.settings, workers=2)
