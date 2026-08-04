from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.collectors.base import CollectionResult, DeviceConnectionSpec
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


def test_scan_service_passes_device_id_to_collector(app):
    collector = RecordingCollector()
    with app.state.session_factory() as session:
        device = Device(
            name="context-server",
            host="10.0.0.18",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(device)
        session.commit()
        device_id = device.id
        service = ScanService(
            session,
            app.state.cipher,
            linux_collector=collector,
            windows_collector=collector,
        )
        service.run(device_id, ScanTrigger.MANUAL)

    assert collector.seen_devices[0].device_id == device_id


def test_scan_service_refuses_marker_before_collector(app):
    collector = RecordingCollector()
    with app.state.session_factory() as session:
        marker = Device(
            name="marker",
            host="10.0.0.91",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(""),
            collection_enabled=False,
        )
        session.add(marker)
        session.commit()
        marker_id = marker.id
        with pytest.raises(CollectionDisabled, match="仅用于集群标注"):
            ScanService(
                session,
                app.state.cipher,
                linux_collector=collector,
                windows_collector=collector,
            ).run(marker_id, ScanTrigger.MANUAL)
        assert session.scalar(
            select(func.count()).select_from(ScanRun).where(
                ScanRun.device_id == marker_id
            )
        ) == 0
    assert collector.seen_devices == []
