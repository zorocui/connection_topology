import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.config import Settings
from app.database import assert_database_current


def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


@pytest.mark.migration
def test_empty_postgresql_database_upgrades_to_head(
    test_database_url,
    migrated_engine,
):
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        script = ScriptDirectory.from_config(config)
        with migrated_engine.connect() as connection:
            assert (
                MigrationContext.configure(connection).get_current_revision()
                == script.get_current_head()
            )
            tables = set(inspect(connection).get_table_names())
            assert {"devices", "scan_tasks", "scan_runs", "import_row_results"} <= tables
            columns = {column["name"] for column in inspect(connection).get_columns("scan_tasks")}
            assert {
                "worker_id",
                "lease_expires_at",
                "heartbeat_at",
                "attempt_count",
            } <= columns
            import_columns = {
                column["name"] for column in inspect(connection).get_columns("import_row_results")
            }
            assert {
                "test_worker_id",
                "test_lease_expires_at",
                "test_heartbeat_at",
                "test_attempt_count",
            } <= import_columns
    finally:
        command.upgrade(config, "head")


def test_active_scan_task_index_is_partial_postgresql_index(migrated_engine):
    with migrated_engine.connect() as connection:
        definition = connection.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_scan_tasks_device_active'")
        )
    assert definition is not None
    assert "WHERE" in definition
    assert "PENDING" in definition and "RUNNING" in definition


def test_database_version_check_accepts_current_engine(migrated_engine):
    assert_database_current(migrated_engine)


@pytest.mark.migration
def test_database_version_check_rejects_unmigrated_engine(
    test_database_url,
    migrated_engine,
):
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "base")
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            assert_database_current(migrated_engine)
    finally:
        command.upgrade(config, "head")


@pytest.mark.migration
def test_application_startup_does_not_migrate_an_empty_database(
    test_database_url,
    migrated_engine,
    valid_key,
):
    from app.main import create_app

    config = alembic_config(test_database_url)
    settings = Settings(
        app_secret_key=valid_key,
        database_url=test_database_url,
        scheduler_enabled=False,
        scan_max_workers=2,
        import_test_max_workers=2,
        db_pool_size=2,
        db_max_overflow=0,
        db_pool_timeout_seconds=1,
        _env_file=None,
    )
    try:
        command.downgrade(config, "base")
        application = create_app(settings)
        with (
            pytest.raises(RuntimeError, match="alembic upgrade head"),
            TestClient(application),
        ):
            pass
        with migrated_engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
        assert "devices" not in tables
        assert "scan_tasks" not in tables
    finally:
        command.upgrade(config, "head")
