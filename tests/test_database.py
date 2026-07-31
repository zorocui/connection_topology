from sqlalchemy import text

from app.database import create_database_engine


def test_sqlite_concurrency_pragmas(tmp_path):
    engine = create_database_engine(
        f"sqlite:///{tmp_path / 'pragmas.db'}",
        sqlite_busy_timeout_ms=12345,
    )
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 12345
        assert connection.execute(text("PRAGMA temp_store")).scalar() == 2
        assert connection.execute(text("PRAGMA cache_size")).scalar() == -65536
        assert connection.execute(text("PRAGMA mmap_size")).scalar() == 268435456
