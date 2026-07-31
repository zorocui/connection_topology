from collections.abc import Generator

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database_engine(
    database_url: str,
    sqlite_busy_timeout_ms: int = 30000,
    pool_size: int = 20,
    max_overflow: int = 10,
    pool_timeout_seconds: int = 60,
) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    connect_args = (
        {
            "check_same_thread": False,
            "timeout": sqlite_busy_timeout_ms / 1000,
        }
        if is_sqlite
        else {}
    )
    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout_seconds,
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={sqlite_busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA cache_size=-65536")
            cursor.execute("PRAGMA mmap_size=268435456")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_database(engine: Engine) -> None:
    from app import models  # noqa: F401
    from app.migrations import run_migrations

    Base.metadata.create_all(engine)
    run_migrations(engine)


def get_db(request: Request) -> Generator[Session, None, None]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
