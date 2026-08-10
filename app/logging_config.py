"""Centralized logging setup: detailed format, rotating .log files, secret redaction."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings
from app.security import SecretRedactingFilter
from app.timezone import BEIJING_TIMEZONE

LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | pid %(process)d | %(threadName)s | "
    "%(name)s | %(filename)s:%(lineno)d | %(message)s"
)
ACCESS_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | pid %(process)d | %(message)s"

APP_LOG_FILENAME = "app.log"
ACCESS_LOG_FILENAME = "access.log"

_HANDLER_MARK = "_connection_topology_handler"


class BeijingTimeFormatter(logging.Formatter):
    """Formats record time as Asia/Shanghai wall clock with milliseconds and offset."""

    default_msec_format = None

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, tz=BEIJING_TIMEZONE)
        base = moment.strftime(datefmt or "%Y-%m-%d %H:%M:%S")
        return f"{base}.{int(record.msecs):03d} +08:00"


def _build_file_handler(
    path: Path,
    settings: Settings,
    formatter: logging.Formatter,
    redactor: logging.Filter,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.addFilter(redactor)
    setattr(handler, _HANDLER_MARK, True)
    return handler


def _drop_managed_handlers(logger: logging.Logger, *, remove_all: bool = False) -> None:
    for handler in list(logger.handlers):
        if remove_all or getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()


def configure_logging(settings: Settings) -> Path:
    """Configure root/uvicorn loggers with console + rotating file handlers.

    Idempotent: re-running replaces handlers created by previous runs, so
    repeated create_app() calls in tests or uvicorn reloads do not duplicate
    output. Returns the directory holding the log files.
    """
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = BeijingTimeFormatter(LOG_FORMAT)
    access_formatter = BeijingTimeFormatter(ACCESS_LOG_FORMAT)
    redactor = SecretRedactingFilter()

    root = logging.getLogger()
    _drop_managed_handlers(root)
    root.setLevel(settings.log_level)
    # Defense in depth: foreign handlers (pytest, embedding apps) also redact.
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(redactor)

    console_handler: logging.Handler | None = None
    if settings.log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(redactor)
        setattr(console_handler, _HANDLER_MARK, True)
        root.addHandler(console_handler)

    root.addHandler(
        _build_file_handler(log_dir / APP_LOG_FILENAME, settings, formatter, redactor)
    )

    # Route uvicorn's own logs through the root handlers with our format.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        _drop_managed_handlers(logger, remove_all=True)
        logger.propagate = True

    # Access logs go to their own file (plus console), not into app.log.
    access_logger = logging.getLogger("uvicorn.access")
    _drop_managed_handlers(access_logger, remove_all=True)
    access_logger.propagate = False
    access_logger.addHandler(
        _build_file_handler(
            log_dir / ACCESS_LOG_FILENAME, settings, access_formatter, redactor
        )
    )
    if console_handler is not None:
        access_logger.addHandler(console_handler)

    return log_dir
