from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import (
    ConnectionRecord,
    ConnectionServiceObservation,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.scans import ScanOutcome, add_scan_outcome
from app.services.scheduler import purge_expired_scans
from app.services.service_observations import (
    backfill_service_observations,
    keep_connection_record,
    sync_service_observations,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)


def add_device(session, app, name="source", host="10.0.0.10"):
    device = Device(
        name=name,
        host=host,
        os_type=OSType.LINUX,
        port=22,
        username=name,
        encrypted_password=app.state.cipher.encrypt("secret"),
    )
    session.add(device)
    session.flush()
    return device


def add_scan(session, device, *, started_at=NOW, status=ScanStatus.SUCCESS):
    scan = ScanRun(
        device_id=device.id,
        trigger_type=ScanTrigger.MANUAL,
        status=status,
        started_at=started_at,
        finished_at=started_at,
        connection_count=0,
    )
    session.add(scan)
    session.flush()
    return scan


def add_record(
    session,
    scan,
    device,
    *,
    local_port=50000,
    pid=100,
    remote_ip="203.0.113.8",
    remote_port=443,
    process_name="client",
    state="ESTABLISHED",
    remote_hostname=None,
):
    record = ConnectionRecord(
        scan_run_id=scan.id,
        protocol="tcp",
        address_family="ipv4",
        local_ip=device.host,
        local_port=local_port,
        remote_ip=remote_ip,
        remote_port=remote_port,
        state=state,
        pid=pid,
        process_name=process_name,
        remote_hostname=remote_hostname,
    )
    session.add(record)
    session.flush()
    return record


def observation_count(session, scan_id):
    return session.scalar(
        select(func.count(ConnectionServiceObservation.id)).where(
            ConnectionServiceObservation.scan_run_id == scan_id
        )
    )


def make_connection(**overrides):
    from app.collectors.base import NormalizedConnection

    values = {
        "protocol": "tcp",
        "address_family": "ipv4",
        "local_ip": "10.0.0.10",
        "local_port": 50000,
        "remote_ip": "203.0.113.8",
        "remote_port": 443,
        "state": "ESTABLISHED",
        "pid": 100,
        "process_name": "client",
    }
    values.update(overrides)
    return NormalizedConnection(**values)


def test_keep_connection_record_rules():
    assert keep_connection_record(make_connection(state="ESTAB")) is True
    assert keep_connection_record(make_connection(state="ESTABLISHED")) is True
    assert keep_connection_record(make_connection(state="TIME-WAIT")) is False
    assert keep_connection_record(make_connection(state="CLOSE-WAIT")) is False
    assert keep_connection_record(make_connection(state="SYN-SENT")) is False
    assert keep_connection_record(make_connection(state=None)) is False
    assert (
        keep_connection_record(make_connection(protocol="udp", state=None))
        is True
    )
    assert (
        keep_connection_record(
            make_connection(state="LISTEN", remote_ip=None, remote_port=None)
        )
        is True
    )


def test_add_scan_outcome_filters_records_and_syncs_observations(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        outcome = ScanOutcome(
            device_id=device.id,
            trigger=ScanTrigger.MANUAL,
            status=ScanStatus.SUCCESS,
            started_at=NOW,
            finished_at=NOW,
            connections=(
                make_connection(state="ESTAB", local_port=50000),
                make_connection(state="TIME-WAIT", local_port=50001),
                make_connection(state="CLOSE-WAIT", local_port=50002),
                make_connection(
                    protocol="udp", state=None, local_port=50003
                ),
                make_connection(
                    state="LISTEN",
                    remote_ip=None,
                    remote_port=None,
                    local_port=22,
                    process_name="sshd",
                ),
            ),
        )

        run = add_scan_outcome(session, outcome)
        session.commit()

        records = session.scalars(
            select(ConnectionRecord)
            .where(ConnectionRecord.scan_run_id == run.id)
            .order_by(ConnectionRecord.local_port)
        ).all()
        assert [record.local_port for record in records] == [22, 50000, 50003]
        assert run.connection_count == 3

        observations = session.scalars(
            select(ConnectionServiceObservation)
            .where(ConnectionServiceObservation.scan_run_id == run.id)
            .order_by(ConnectionServiceObservation.protocol)
        ).all()
        assert [row.protocol for row in observations] == ["tcp", "udp"]
        assert all(row.remote_ip == "203.0.113.8" for row in observations)
        assert all(row.sample_connection_id is not None for row in observations)


def test_failed_scan_outcome_creates_no_observations(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        outcome = ScanOutcome(
            device_id=device.id,
            trigger=ScanTrigger.MANUAL,
            status=ScanStatus.FAILED,
            started_at=NOW,
            finished_at=NOW,
            error_code="connection_failed",
            error_message="unreachable",
        )

        run = add_scan_outcome(session, outcome)
        session.commit()

        assert observation_count(session, run.id) == 0


def test_observation_keeps_latest_sample_and_latest_non_null_hostname(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        scan = add_scan(session, device)
        add_record(session, scan, device, local_port=50000, pid=100)
        add_record(
            session,
            scan,
            device,
            local_port=50001,
            pid=101,
            remote_hostname="peer.internal",
        )
        latest = add_record(session, scan, device, local_port=50002, pid=102)

        sync_service_observations(session, scan)
        session.commit()

        row = session.scalars(
            select(ConnectionServiceObservation).where(
                ConnectionServiceObservation.scan_run_id == scan.id
            )
        ).one()
        assert row.sample_connection_id == latest.id
        assert row.remote_hostname == "peer.internal"
        assert set(row.local_ports.split(",")) == {"50000", "50001", "50002"}
        assert set(row.pids.split(",")) == {"100", "101", "102"}
        assert row.local_ips == device.host
        assert row.device_id == device.id
        assert row.started_at.astimezone(timezone.utc) == NOW


def test_sync_service_observations_is_idempotent(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        scan = add_scan(session, device)
        add_record(session, scan, device)

        sync_service_observations(session, scan)
        sync_service_observations(session, scan)
        session.commit()

        assert observation_count(session, scan.id) == 1


def test_backfill_populates_missing_observations_in_batches(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        first = add_scan(session, device)
        add_record(session, first, device, local_port=50000)
        second = add_scan(
            session, device, started_at=NOW + timedelta(hours=1)
        )
        add_record(session, second, device, local_port=50001)

        backfill_service_observations(
            session.connection(), scan_id_batch_size=1
        )
        backfill_service_observations(
            session.connection(), scan_id_batch_size=1
        )
        session.commit()

        assert observation_count(session, first.id) == 1
        assert observation_count(session, second.id) == 1
        total = session.scalar(
            select(func.count(ConnectionServiceObservation.id))
        )
        assert total == 2


def test_purge_expired_scans_cascades_observations(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        expired = add_scan(
            session, device, started_at=NOW - timedelta(days=8)
        )
        add_record(session, expired, device)
        recent = add_scan(session, device)
        add_record(session, recent, device)
        sync_service_observations(session, expired)
        sync_service_observations(session, recent)
        session.commit()

        assert purge_expired_scans(session, 7, now=NOW) == 1

        assert observation_count(session, expired.id) == 0
        assert observation_count(session, recent.id) == 1
