from fastapi.testclient import TestClient


def test_lifespan_starts_and_stops_postgresql_services(app, monkeypatch):
    calls = []
    for name in ("scan_queue", "import_test_service", "topology_listener"):
        service = getattr(app.state, name)
        monkeypatch.setattr(service, "start", lambda name=name: calls.append(f"start:{name}"))
        monkeypatch.setattr(
            service, "shutdown", lambda name=name: calls.append(f"stop:{name}")
        )
    with TestClient(app):
        assert calls[:3] == [
            "start:scan_queue",
            "start:import_test_service",
            "start:topology_listener",
        ]
    assert calls[-3:] == [
        "stop:import_test_service",
        "stop:scan_queue",
        "stop:topology_listener",
    ]
