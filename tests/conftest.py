import os
from dataclasses import dataclass

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient

from app.collectors.base import CollectionResult, NormalizedConnection
from app.config import Settings
from app.database import init_database
from app.main import create_app
from app.models import OSType


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
def app(tmp_path, valid_key):
    settings = Settings(
        app_secret_key=valid_key,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        scheduler_enabled=False,
        scan_max_workers=2,
        import_test_max_workers=2,
        db_pool_size=2,
        db_max_overflow=0,
        db_pool_timeout_seconds=1,
        _env_file=None,
    )
    application = create_app(settings)
    init_database(application.state.engine)
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
