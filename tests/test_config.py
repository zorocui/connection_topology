import pytest
from pydantic import ValidationError

from app.config import Settings

POSTGRESQL_URL = "postgresql+psycopg://app:secret@localhost/app"


def test_settings_reject_missing_secret(monkeypatch):
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(database_url=POSTGRESQL_URL, _env_file=None)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./old.db",
        "postgresql://user:pass@localhost/app",
        "mysql+pymysql://user:pass@localhost/app",
    ],
)
def test_settings_accept_only_psycopg_postgresql_urls(valid_key, url):
    with pytest.raises(ValidationError):
        Settings(app_secret_key=valid_key, database_url=url, _env_file=None)


def test_postgresql_defaults(valid_key):
    settings = Settings(
        app_secret_key=valid_key,
        database_url=POSTGRESQL_URL,
        _env_file=None,
    )
    assert settings.host == "127.0.0.1"
    assert settings.history_retention_days == 7
    assert settings.import_test_max_workers == 20
    assert settings.scan_max_workers == 30
    assert settings.scan_queue_size == 2000
    assert settings.scan_jitter_seconds == 300
    assert settings.web_workers is None
    assert settings.db_pool_size == 3
    assert settings.db_max_overflow == 2
    assert settings.db_pool_timeout_seconds == 30
    assert settings.db_pool_recycle_seconds == 1800
    assert settings.scan_lease_seconds == 90
    assert settings.task_heartbeat_seconds == 15
    assert not hasattr(settings, "sqlite_busy_timeout_ms")
    assert not hasattr(settings, "sqlite_write_retry_delays")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_pool_size", 0),
        ("db_pool_size", 51),
        ("db_max_overflow", -1),
        ("db_max_overflow", 51),
        ("db_pool_timeout_seconds", 0),
        ("db_pool_timeout_seconds", 301),
        ("db_pool_recycle_seconds", 59),
        ("db_pool_recycle_seconds", 86401),
    ],
)
def test_settings_reject_invalid_database_pool_values(valid_key, field, value):
    with pytest.raises(ValidationError):
        Settings(
            app_secret_key=valid_key,
            database_url=POSTGRESQL_URL,
            _env_file=None,
            **{field: value},
        )


def test_task_heartbeat_must_be_less_than_half_the_scan_lease(valid_key):
    with pytest.raises(ValidationError):
        Settings(
            app_secret_key=valid_key,
            database_url=POSTGRESQL_URL,
            scan_lease_seconds=30,
            task_heartbeat_seconds=15,
            _env_file=None,
        )


def test_blank_web_workers_uses_cpu_derived_default(valid_key, monkeypatch):
    monkeypatch.setenv("WEB_WORKERS", "")
    settings = Settings(
        app_secret_key=valid_key,
        database_url=POSTGRESQL_URL,
        _env_file=None,
    )
    assert settings.web_workers is None
