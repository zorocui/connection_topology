from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.collectors.base import CollectionResult, DeviceConnectionSpec
from app.models import Device, OSType, ScanRun, ScanStatus, ScanTrigger
from app.services.scheduler import purge_expired_scans
from app.services.scans import ScanService


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
