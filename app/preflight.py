from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.config import Settings, get_settings
from app.database import assert_database_current, create_database_engine


@dataclass(frozen=True)
class PostgreSQLLimits:
    server_version: str
    max_connections: int


@dataclass(frozen=True)
class PreflightReport:
    server_version: str
    workers: int
    requested_connections: int
    available_connections: int
    migration: str = "current"


def load_postgresql_limits(engine: Engine) -> PostgreSQLLimits:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT current_setting('server_version'), "
                "current_setting('max_connections')::integer"
            )
        ).one()
    return PostgreSQLLimits(server_version=row[0], max_connections=row[1])


def run_preflight(settings: Settings, workers: int) -> PreflightReport:
    if workers < 1:
        raise ValueError("workers must be positive")
    engine = create_database_engine(settings)
    try:
        limits = load_postgresql_limits(engine)
        major = limits.server_version.split(".", 1)[0]
        if major != "15":
            raise RuntimeError("仅支持 PostgreSQL 15")
        assert_database_current(engine)
        requested = workers * (
            settings.db_pool_size + settings.db_max_overflow + 2
        )
        available = max(limits.max_connections - 10, 0)
        if requested > available:
            raise RuntimeError(
                f"连接数预算超限：需要 {requested}，可用 {available}"
            )
        return PreflightReport(
            server_version=limits.server_version,
            workers=workers,
            requested_connections=requested,
            available_connections=available,
        )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL runtime preflight")
    parser.add_argument("--workers", type=int, required=True)
    arguments = parser.parse_args()
    report = run_preflight(get_settings(), arguments.workers)
    print(
        "PostgreSQL preflight ok "
        f"version={report.server_version} workers={report.workers} "
        f"connections={report.requested_connections}/{report.available_connections} "
        f"migration={report.migration}"
    )


if __name__ == "__main__":
    main()
