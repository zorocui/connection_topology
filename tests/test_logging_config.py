import logging
import re
from logging.handlers import RotatingFileHandler

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logging_config import (
    ACCESS_LOG_FILENAME,
    APP_LOG_FILENAME,
    BeijingTimeFormatter,
    configure_logging,
)

POSTGRESQL_URL = "postgresql+psycopg://app:secret@localhost/app"


def make_settings(valid_key, **overrides) -> Settings:
    base = {
        "app_secret_key": valid_key,
        "database_url": POSTGRESQL_URL,
        "_env_file": None,
        "log_to_console": False,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def close_managed_handlers():
    yield
    for name in (None, "uvicorn.access"):
        target = logging.getLogger() if name is None else logging.getLogger(name)
        for handler in list(target.handlers):
            if getattr(handler, "_connection_topology_handler", False):
                target.removeHandler(handler)
                handler.close()


def managed_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        handler
        for handler in logger.handlers
        if getattr(handler, "_connection_topology_handler", False)
    ]


def test_logging_settings_defaults(valid_key):
    settings = Settings(
        app_secret_key=valid_key,
        database_url=POSTGRESQL_URL,
        _env_file=None,
    )
    assert settings.log_level == "INFO"
    assert settings.log_dir == "logs"
    assert settings.log_max_bytes == 10 * 1024 * 1024
    assert settings.log_backup_count == 5
    assert settings.log_to_console is True


def test_log_level_is_normalized(valid_key):
    settings = make_settings(valid_key, log_level=" debug ")
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("log_level", "VERBOSE"),
        ("log_max_bytes", 1024),
        ("log_max_bytes", 2 * 1024 * 1024 * 1024),
        ("log_backup_count", 0),
        ("log_backup_count", 101),
    ],
)
def test_logging_settings_reject_invalid_values(valid_key, field, value):
    with pytest.raises(ValidationError):
        make_settings(valid_key, **{field: value})


def test_formatter_uses_beijing_wall_clock():
    record = logging.LogRecord("demo", logging.INFO, __file__, 1, "msg", (), None)
    record.created = 0
    record.msecs = 0
    formatter = BeijingTimeFormatter()
    assert formatter.formatTime(record) == "1970-01-01 08:00:00.000 +08:00"


def test_configure_logging_writes_detailed_app_log(valid_key, tmp_path):
    settings = make_settings(valid_key, log_dir=str(tmp_path))
    log_dir = configure_logging(settings)

    assert log_dir == tmp_path
    logging.getLogger("demo.app").info("hello topology")

    content = (tmp_path / APP_LOG_FILENAME).read_text(encoding="utf-8")
    pattern = (
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \+08:00 \| INFO     \| "
        r"pid \d+ \| MainThread \| demo\.app \| test_logging_config\.py:\d+ \| "
        r"hello topology"
    )
    assert re.search(pattern, content)


def test_access_log_goes_to_separate_file(valid_key, tmp_path):
    settings = make_settings(valid_key, log_dir=str(tmp_path))
    configure_logging(settings)

    logging.getLogger("uvicorn.access").info('GET /api/health HTTP/1.1" 200')

    access_content = (tmp_path / ACCESS_LOG_FILENAME).read_text(encoding="utf-8")
    app_content = (tmp_path / APP_LOG_FILENAME).read_text(encoding="utf-8")
    assert "GET /api/health" in access_content
    assert "GET /api/health" not in app_content


def test_uvicorn_error_propagates_to_app_log(valid_key, tmp_path):
    settings = make_settings(valid_key, log_dir=str(tmp_path))
    configure_logging(settings)

    logging.getLogger("uvicorn.error").error("worker boom")

    content = (tmp_path / APP_LOG_FILENAME).read_text(encoding="utf-8")
    assert "worker boom" in content


def test_secrets_are_redacted_in_log_files(valid_key, tmp_path):
    settings = make_settings(valid_key, log_dir=str(tmp_path))
    configure_logging(settings)

    logging.getLogger("demo.app").error("连接失败 password=TopSecret123 无法认证")

    content = (tmp_path / APP_LOG_FILENAME).read_text(encoding="utf-8")
    assert "password=[REDACTED]" in content
    assert "TopSecret123" not in content


def test_reconfigure_does_not_duplicate_handlers_or_lines(valid_key, tmp_path):
    settings = make_settings(valid_key, log_dir=str(tmp_path))
    configure_logging(settings)
    configure_logging(settings)

    root = logging.getLogger()
    assert len(managed_handlers(root)) == 1  # file handler only, console disabled
    assert len(managed_handlers(logging.getLogger("uvicorn.access"))) == 1

    logging.getLogger("demo.app").info("exactly once")

    content = (tmp_path / APP_LOG_FILENAME).read_text(encoding="utf-8")
    assert content.count("exactly once") == 1


def test_log_level_filters_lower_levels(valid_key, tmp_path):
    settings = make_settings(valid_key, log_dir=str(tmp_path), log_level="WARNING")
    configure_logging(settings)

    logger = logging.getLogger("demo.app")
    logger.info("hidden detail")
    logger.warning("visible problem")

    content = (tmp_path / APP_LOG_FILENAME).read_text(encoding="utf-8")
    assert "hidden detail" not in content
    assert "visible problem" in content


def test_rotation_limits_applied_to_file_handlers(valid_key, tmp_path):
    settings = make_settings(
        valid_key,
        log_dir=str(tmp_path),
        log_max_bytes=1024 * 1024,
        log_backup_count=2,
    )
    configure_logging(settings)

    file_handlers = [
        handler
        for handler in managed_handlers(logging.getLogger())
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 1024 * 1024
    assert file_handlers[0].backupCount == 2


def test_console_handler_added_when_enabled(valid_key, tmp_path):
    settings = make_settings(valid_key, log_dir=str(tmp_path), log_to_console=True)
    configure_logging(settings)

    root_handlers = managed_handlers(logging.getLogger())
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root_handlers
    )
    access_handlers = managed_handlers(logging.getLogger("uvicorn.access"))
    assert len(access_handlers) == 2
