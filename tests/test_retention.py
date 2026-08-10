from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import (
    Cluster,
    ConnectionRecord,
    ConnectionServiceObservation,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.retention import resolve_device_retention
from app.services.scheduler import (
    purge_expired_scans,
    purge_raw_connection_records,
)
from app.services.service_observations import sync_service_observations

NOW = datetime(2026, 7, 31, 3, 0, tzinfo=timezone.utc)


def _device(app, name, *, cluster=None, retention_days=None):
    return Device(
        name=name,
        host=f"10.0.0.{len(name)}",
        os_type=OSType.LINUX,
        port=22,
        username=name,
        encrypted_password=app.state.cipher.encrypt("secret"),
        cluster=cluster,
        history_retention_days=retention_days,
    )


def _scan(device, age_days, *, with_connection=False):
    scan = ScanRun(
        device=device,
        trigger_type=ScanTrigger.MANUAL,
        status=ScanStatus.SUCCESS,
        started_at=NOW - timedelta(days=age_days),
    )
    if with_connection:
        scan.connections.append(
            ConnectionRecord(
                protocol="tcp",
                address_family="ipv4",
                local_ip=device.host,
                local_port=50000,
                remote_ip="203.0.113.8",
                remote_port=443,
            )
        )
    return scan


def test_retention_priority_device_cluster_system(app):
    cluster = Cluster(name="cluster", history_retention_days=14)
    inherited = _device(app, "inherited", cluster=cluster)
    overridden = _device(
        app,
        "overridden",
        cluster=cluster,
        retention_days=30,
    )
    system = _device(app, "system")

    assert resolve_device_retention(overridden, 7).days == 30
    assert resolve_device_retention(overridden, 7).source == "device"
    assert resolve_device_retention(inherited, 7).days == 14
    assert resolve_device_retention(inherited, 7).source == "cluster"
    assert resolve_device_retention(system, 7).days == 7
    assert resolve_device_retention(system, 7).source == "system"


def test_purge_expired_scans_uses_each_device_policy_and_cascades(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="cluster", history_retention_days=14)
        system = _device(app, "system")
        inherited = _device(app, "inherited", cluster=cluster)
        overridden = _device(
            app,
            "overridden",
            cluster=cluster,
            retention_days=30,
        )
        session.add_all([cluster, system, inherited, overridden])
        session.add_all(
            [
                _scan(system, 8, with_connection=True),
                _scan(system, 6),
                _scan(inherited, 15),
                _scan(inherited, 13),
                _scan(overridden, 31),
                _scan(overridden, 29),
            ]
        )
        session.commit()

        assert purge_expired_scans(session, 7, now=NOW, chunk_size=1) == 3
        remaining_ages = sorted(
            (NOW - scan.started_at.replace(tzinfo=timezone.utc)).days
            for scan in session.scalars(select(ScanRun))
        )
        connection_count = session.scalar(
            select(func.count(ConnectionRecord.id))
        )

        assert remaining_ages == [6, 13, 29]
        assert connection_count == 0


def test_purge_raw_connection_records_keeps_baselines_and_observations(app):
    with app.state.session_factory() as session:
        device = _device(app, "rawreten")
        session.add(device)
        scans = [
            _scan(device, 8, with_connection=True),
            _scan(device, 6, with_connection=True),
            _scan(device, 3, with_connection=True),
            _scan(device, 1, with_connection=True),
        ]
        session.add_all(scans)
        session.commit()
        for scan in scans:
            sync_service_observations(session, scan)
        session.commit()

        deleted = purge_raw_connection_records(
            session, 2, now=NOW, chunk_size=1
        )

        # Only the two oldest scans lose their raw rows; the latest two
        # successful scans per device are kept as topology/diff baselines.
        assert deleted == 2
        remaining_record_scan_ids = {
            record.scan_run_id
            for record in session.scalars(select(ConnectionRecord))
        }
        assert remaining_record_scan_ids == {scans[2].id, scans[3].id}
        # Scan runs and observations stay for the full history retention.
        assert session.scalar(select(func.count(ScanRun.id))) == 4
        assert session.scalar(
            select(func.count(ConnectionServiceObservation.id))
        ) == 4
        # Idempotent: a second run finds nothing left to purge.
        assert purge_raw_connection_records(session, 2, now=NOW) == 0
