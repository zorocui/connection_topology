import pytest

from app.collectors import windows
from app.collectors.base import CollectorError, DeviceConnectionSpec
from app.collectors.windows import (
    WINDOWS_COMPONENT_ERROR_CODE,
    WINDOWS_COMPONENT_ERROR_MESSAGE,
    WindowsCollector,
    parse_windows_json,
)
from app.config import Settings
from app.main import create_app
from app.models import Device, OSType, ScanStatus, ScanTrigger
from app.services.scans import ScanService


def unavailable_collector(monkeypatch) -> WindowsCollector:
    monkeypatch.setattr(windows, "winrm", None)
    return WindowsCollector()


@pytest.mark.parametrize("operation", ["test", "collect"])
def test_windows_collector_reports_missing_optional_component(monkeypatch, operation):
    collector = unavailable_collector(monkeypatch)
    device = DeviceConnectionSpec(host="10.0.0.20", port=5985, username="administrator")

    with pytest.raises(CollectorError) as caught:
        if operation == "test":
            collector.test_connection(device, "secret")
        else:
            collector.collect(device, "secret")

    assert caught.value.code == WINDOWS_COMPONENT_ERROR_CODE
    assert str(caught.value) == WINDOWS_COMPONENT_ERROR_MESSAGE


def test_windows_parser_does_not_require_winrm(monkeypatch):
    monkeypatch.setattr(windows, "winrm", None)
    rows = parse_windows_json(
        '[{"Protocol":"tcp","LocalAddress":"10.0.0.20","LocalPort":5985,'
        '"RemoteAddress":"10.0.0.10","RemotePort":50100,"State":"Established",'
        '"OwningProcess":123,"ProcessName":"svchost"}]'
    )
    assert len(rows) == 1
    assert rows[0].state == "ESTABLISHED"


def test_app_creation_does_not_require_winrm(monkeypatch, test_database_url, valid_key):
    monkeypatch.setattr(windows, "winrm", None)
    settings = Settings(
        app_secret_key=valid_key,
        database_url=test_database_url,
        scheduler_enabled=False,
        _env_file=None,
    )

    application = create_app(settings)

    assert application.state.linux_collector is not None
    assert application.state.windows_collector is not None


def test_windows_scan_is_recorded_as_failed_without_component(monkeypatch, app):
    with app.state.session_factory() as session:
        device = Device(
            name="windows-server",
            host="10.0.0.20",
            os_type=OSType.WINDOWS,
            port=5985,
            username="administrator",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(device)
        session.commit()
        session.refresh(device)

        device_id = device.id

    outcome = ScanService(
        app.state.session_factory,
        app.state.cipher,
        linux_collector=app.state.linux_collector,
        windows_collector=unavailable_collector(monkeypatch),
    ).collect(device_id, ScanTrigger.MANUAL)

    assert outcome.status == ScanStatus.FAILED
    assert outcome.error_code == WINDOWS_COMPONENT_ERROR_CODE
    assert outcome.error_message == WINDOWS_COMPONENT_ERROR_MESSAGE
