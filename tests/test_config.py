import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_reject_missing_secret(monkeypatch):
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_defaults_to_loopback(valid_key):
    settings = Settings(app_secret_key=valid_key, _env_file=None)
    assert settings.host == "127.0.0.1"
    assert settings.history_retention_days == 7
    assert settings.import_test_max_workers == 20
    assert settings.scan_max_workers == 30
    assert settings.scan_queue_size == 2000
    assert settings.scan_jitter_seconds == 300
    assert settings.sqlite_busy_timeout_ms == 30000
    assert settings.db_pool_size == 20
    assert settings.db_max_overflow == 10
    assert settings.db_pool_timeout_seconds == 60


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_pool_size", 0),
        ("db_pool_size", 201),
        ("db_max_overflow", -1),
        ("db_max_overflow", 201),
        ("db_pool_timeout_seconds", 0),
        ("db_pool_timeout_seconds", 301),
    ],
)
def test_settings_reject_invalid_database_pool_values(
    valid_key,
    field,
    value,
):
    with pytest.raises(ValidationError):
        Settings(
            app_secret_key=valid_key,
            _env_file=None,
            **{field: value},
        )
