from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import (
    Cluster,
    ConnectionRecord,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.retention import resolve_device_retention
from app.services.scheduler import purge_expired_scans

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

        assert purge_expired_scans(session, 7, now=NOW) == 3
        remaining_ages = sorted(
            (NOW - scan.started_at.replace(tzinfo=timezone.utc)).days
            for scan in session.scalars(select(ScanRun))
        )
        connection_count = session.scalar(
            select(func.count(ConnectionRecord.id))
        )

        assert remaining_ages == [6, 13, 29]
        assert connection_count == 0
