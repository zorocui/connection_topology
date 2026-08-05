from app.runtime import resolve_web_workers


def test_worker_count_uses_override_or_capped_cpu():
    assert resolve_web_workers(3, cpu_count=64) == 3
    assert resolve_web_workers(None, cpu_count=1) == 1
    assert resolve_web_workers(None, cpu_count=6) == 6
    assert resolve_web_workers(None, cpu_count=64) == 8


def test_worker_count_falls_back_to_one_when_cpu_is_unknown(monkeypatch):
    monkeypatch.setattr("app.runtime.os.cpu_count", lambda: None)
    assert resolve_web_workers(None) == 1
