from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.collectors.base import CollectionResult, CollectorError, DeviceConnectionSpec
from app.models import Device, OSType, ScanRun, ScanStatus, ScanTrigger
from app.services.scans import CollectionDisabled, ScanService
from app.services.scheduler import purge_expired_scans


@dataclass
class RecordingCollector:
    seen_devices: list[DeviceConnectionSpec] = field(default_factory=list)

    def collect(self, device, password):
        self.seen_devices.append(device)
        return CollectionResult(())

    def test_connection(self, device, password):
        self.seen_devices.append(device)


class FailingRecordingCollector:
    def __init__(self, error):
        self.error = error
        self.seen_devices = []

    def collect(self, device, password):
        self.seen_devices.append(device)
        raise self.error

    def test_connection(self, device, password):
        self.seen_devices.append(device)
        raise self.error


def seed_service_device(
    app,
    *,
    host,
    collection_enabled=True,
    password="secret",
):
    with app.state.session_factory() as session:
        device = Device(
            name=f"device-{host}",
            host=host,
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(password),
            collection_enabled=collection_enabled,
        )
        session.add(device)
        session.commit()
        return device.id


def test_retention_removes_only_expired_runs(app):
    with app.state.session_factory() as session:
        device = Device(
            name="server",
            host="10.0.0.8",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(device)
        session.flush()
        old = ScanRun(
            device_id=device.id,
            trigger_type=ScanTrigger.MANUAL,
            status=ScanStatus.SUCCESS,
            started_at=datetime.now(timezone.utc) - timedelta(days=31),
        )
        recent = ScanRun(
            device_id=device.id,
            trigger_type=ScanTrigger.MANUAL,
            status=ScanStatus.SUCCESS,
            started_at=datetime.now(timezone.utc),
        )
        session.add_all([old, recent])
        session.commit()
        assert purge_expired_scans(session, 30) == 1
        runs = session.scalars(select(ScanRun)).all()
        assert [run.id for run in runs] == [recent.id]


def test_collect_returns_detached_success_outcome_without_scan_run(app):
    collector = RecordingCollector()
    device_id = seed_service_device(app, host="10.0.0.18")
    service = ScanService(
        app.state.session_factory,
        app.state.cipher,
        linux_collector=collector,
        windows_collector=collector,
    )

    outcome = service.collect(device_id, ScanTrigger.MANUAL)

    assert outcome.device_id == device_id
    assert outcome.status == ScanStatus.SUCCESS
    assert outcome.error_code is None
    assert collector.seen_devices[0].device_id == device_id
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 0


def test_collect_converts_collector_error_without_writing(app):
    collector = FailingRecordingCollector(
        CollectorError("authentication_failed", "认证失败")
    )
    device_id = seed_service_device(app, host="10.0.0.19")
    outcome = ScanService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
    ).collect(device_id, ScanTrigger.MANUAL)
    assert outcome.status == ScanStatus.FAILED
    assert outcome.error_code == "authentication_failed"
    assert outcome.error_message == "认证失败"
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 0


def test_collect_refuses_marker_before_collector(app):
    collector = RecordingCollector()
    marker_id = seed_service_device(
        app,
        host="10.0.0.20",
        collection_enabled=False,
        password="",
    )
    with pytest.raises(CollectionDisabled, match="仅用于集群标注"):
        ScanService(
            app.state.session_factory,
            app.state.cipher,
            collector,
            collector,
        ).collect(marker_id, ScanTrigger.MANUAL)
    assert collector.seen_devices == []
