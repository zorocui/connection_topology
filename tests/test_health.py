from contextlib import contextmanager


def test_health_reports_database_and_migration_ready(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"database": "ok", "migration": "current"}


def test_health_returns_sanitized_503_when_database_fails(client, app, monkeypatch):
    class BrokenSession:
        def execute(self, statement):
            del statement
            raise RuntimeError("password=raw-secret database_url=private")

    @contextmanager
    def broken_factory():
        yield BrokenSession()

    monkeypatch.setattr(app.state, "session_factory", broken_factory)
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "数据库不可用"}
    assert "raw-secret" not in response.text
