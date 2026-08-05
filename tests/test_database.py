from unittest.mock import sentinel

from app.config import Settings
from app.database import create_database_engine


def test_database_engine_uses_postgresql_health_settings(monkeypatch, valid_key):
    settings = Settings(
        app_secret_key=valid_key,
        database_url="postgresql+psycopg://app:secret@localhost/app",
        _env_file=None,
    )
    calls = []

    def fake_create_engine(url, **kwargs):
        calls.append((url, kwargs))
        return sentinel.engine

    monkeypatch.setattr("app.database.create_engine", fake_create_engine)

    assert create_database_engine(settings) is sentinel.engine
    assert calls == [
        (
            settings.database_url,
            {
                "pool_size": 3,
                "max_overflow": 2,
                "pool_timeout": 30,
                "pool_recycle": 1800,
                "pool_pre_ping": True,
                "connect_args": {"connect_timeout": 10},
            },
        )
    ]
