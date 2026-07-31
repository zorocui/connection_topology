from sqlalchemy import text

from app.database import create_database_engine


def test_database_engine_uses_configured_queue_pool(tmp_path):
    engine = create_database_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        sqlite_busy_timeout_ms=45000,
        pool_size=7,
        max_overflow=3,
        pool_timeout_seconds=12,
    )
    try:
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 3
        assert engine.pool._timeout == 12
        with engine.connect() as connection:
            assert (
                connection.execute(text("PRAGMA busy_timeout")).scalar_one()
                == 45000
            )
            assert (
                connection.execute(text("PRAGMA journal_mode"))
                .scalar_one()
                .lower()
                == "wal"
            )
    finally:
        engine.dispose()
