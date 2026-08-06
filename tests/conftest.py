import os
from dataclasses import dataclass

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient

from app.collectors.base import CollectionResult, NormalizedConnection
from app.config import Settings
from app.database import Base
from app.models import OSType, SystemSetting


class TestDatabaseSettings(BaseSettings):
    test_database_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )


def _assert_test_database_url(database_url: str) -> None:
    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg":
        raise RuntimeError("TEST_DATABASE_URL must use postgresql+psycopg")
    if url.database != "connection_topology_test":
        raise RuntimeError("Refusing to use a test database other than connection_topology_test")


def _load_test_database_url(*, env_file: str | None = ".env") -> str:
    try:
        database_url = TestDatabaseSettings(_env_file=env_file).test_database_url
    except ValidationError as exc:
        raise RuntimeError("TEST_DATABASE_URL is required for PostgreSQL tests") from exc
    _assert_test_database_url(database_url)
    return database_url


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return _load_test_database_url()


@pytest.fixture(scope="session")
def migrated_engine(test_database_url):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    command.upgrade(config, "head")
    engine = create_engine(test_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            database_name = connection.scalar(text("SELECT current_database()"))
        if database_name != "connection_topology_test":
            raise RuntimeError(
                "Refusing to use a test database other than connection_topology_test"
            )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(request, migrated_engine):
    if request.node.get_closest_marker("migration") is not None:
        yield
        return

    table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
    quoted_names = ", ".join(f'"{name}"' for name in table_names)
    with migrated_engine.begin() as connection:
        database_name = connection.scalar(text("SELECT current_database()"))
        if database_name != "connection_topology_test":
            raise RuntimeError(
                "Refusing to truncate a database other than connection_topology_test"
            )
        connection.execute(text(f"TRUNCATE TABLE {quoted_names} RESTART IDENTITY CASCADE"))
        connection.execute(SystemSetting.__table__.insert().values(id=1, history_retention_days=7))
    yield


@pytest.fixture
def valid_key() -> str:
    return Fernet.generate_key().decode()


@dataclass
class FakeCollector:
    fail: Exception | None = None

    def test_connection(self, device, password: str) -> None:
        if self.fail:
            raise self.fail

    def collect(self, device, password: str) -> CollectionResult:
        if self.fail:
            raise self.fail
        return CollectionResult(
            (
                NormalizedConnection(
                    protocol="tcp",
                    address_family="ipv4",
                    local_ip="10.0.0.10",
                    local_port=50124,
                    remote_ip="10.0.0.20",
                    remote_port=443,
                    state="ESTABLISHED",
                    pid=1024,
                    process_name="curl",
                ),
                NormalizedConnection(
                    protocol="tcp",
                    address_family="ipv4",
                    local_ip="0.0.0.0",
                    local_port=22,
                    remote_ip=None,
                    remote_port=None,
                    state="LISTEN",
                    pid=778,
                    process_name="sshd",
                ),
            )
        )


@pytest.fixture
def app(test_database_url, valid_key):
    from app.main import create_app

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
    application = create_app(settings)
    application.state.linux_collector = FakeCollector()
    application.state.windows_collector = FakeCollector()
    application.state.scan_queue.linux_collector = application.state.linux_collector
    application.state.scan_queue.windows_collector = application.state.windows_collector
    application.state.scan_queue.scan_service.collectors[OSType.LINUX] = (
        application.state.linux_collector
    )
    application.state.scan_queue.scan_service.collectors[OSType.WINDOWS] = (
        application.state.windows_collector
    )
    application.state.import_test_service.linux_collector = application.state.linux_collector
    application.state.import_test_service.windows_collector = application.state.windows_collector
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def linux_device_payload():
    return {
        "name": "生产 Web 01",
        "host": "10.0.0.10",
        "os_type": "linux",
        "port": 22,
        "username": "ops",
        "password": "TopSecret!",
        "scan_interval_minutes": 5,
        "scheduled_enabled": True,
    }
