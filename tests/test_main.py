from fastapi.testclient import TestClient


def test_lifespan_acquires_and_releases_sqlite_process_guard(app, monkeypatch):
    calls = []
    monkeypatch.setattr(
        app.state.sqlite_process_guard,
        "acquire",
        lambda: calls.append("acquire"),
    )
    monkeypatch.setattr(
        app.state.sqlite_process_guard,
        "release",
        lambda: calls.append("release"),
    )
    with TestClient(app):
        assert calls == ["acquire"]
    assert calls == ["acquire", "release"]
