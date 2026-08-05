from unittest.mock import sentinel

from app.config import Settings
from app.database import create_database_engine


def test_database_engine_uses_configured_pool_values(monkeypatch, valid_key):
    settings = Settings(
        app_secret_key=valid_key,
        database_url="postgresql+psycopg://app:secret@localhost/app",
        db_pool_size=7,
        db_max_overflow=3,
        db_pool_timeout_seconds=12,
        db_pool_recycle_seconds=600,
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
                "pool_size": 7,
                "max_overflow": 3,
                "pool_timeout": 12,
                "pool_recycle": 600,
                "pool_pre_ping": True,
                "connect_args": {"connect_timeout": 10},
            },
        )
    ]
