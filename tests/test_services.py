from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Device, OSType, ScanRun, ScanStatus, ScanTrigger
from app.services.scheduler import purge_expired_scans


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
