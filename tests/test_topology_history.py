from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.models import (
    ConnectionRecord,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.service_observations import sync_service_observations
from app.services.topology_history import (
    WINDOW_DELTAS,
    aggregate_current_connections,
    aggregate_historical_connections,
    aggregate_service_connections,
    load_current_scans,
)

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)


def service_projection(rows):
    keys = (
        "source_device_id",
        "protocol",
        "remote_ip",
        "remote_port",
        "process_name",
        "is_current",
        "first_seen",
        "last_seen",
        "observation_count",
        "observed_local_ips",
        "observed_local_ports",
        "observed_pids",
    )
    return sorted(
        [{key: row[key] for key in keys} for row in rows],
        key=lambda row: (
            row["source_device_id"],
            row["protocol"],
            row["remote_ip"],
            row["remote_port"],
            row["process_name"],
        ),
    )


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


def add_scan(
    session,
    device,
    *,
    started_at,
    status=ScanStatus.SUCCESS,
    local_port=50000,
    pid=100,
    remote_ip="203.0.113.8",
    remote_port=443,
    process_name="client",
):
    scan = ScanRun(
        device_id=device.id,
        trigger_type=ScanTrigger.MANUAL,
        status=status,
        started_at=started_at,
        finished_at=started_at,
        connection_count=1,
    )
    session.add(scan)
    session.flush()
    scan.connections.append(
        ConnectionRecord(
            protocol="tcp",
            address_family="ipv4",
            local_ip=device.host,
            local_port=local_port,
            remote_ip=remote_ip,
            remote_port=remote_port,
            state="ESTABLISHED",
            pid=pid,
            process_name=process_name,
        )
    )
    session.flush()
    return scan


def sync_observations(session, *scans):
    session.flush()
    for scan in scans:
        sync_service_observations(session, scan)


def test_load_current_scans_can_skip_connections(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        current = add_scan(session, device, started_at=NOW)
        session.commit()
        current_id = current.id
        device_id = device.id
        session.expunge_all()

        loaded = load_current_scans(
            session,
            [device_id],
            with_connections=False,
        )

        assert loaded[device_id].id == current_id
        assert "connections" not in loaded[device_id].__dict__


def test_current_sql_filters_to_outbound_and_inbound_candidates(app):
    with app.state.session_factory() as session:
        selected = add_device(session, app, "selected-current", "10.0.0.10")
        inbound = add_device(session, app, "inbound-current", "10.0.0.20")
        unrelated = add_device(session, app, "unrelated-current", "10.0.0.30")
        selected_scan = add_scan(
            session,
            selected,
            started_at=NOW,
            remote_ip=selected.host,
        )
        inbound_scan = add_scan(
            session,
            inbound,
            started_at=NOW,
            remote_ip=selected.host,
        )
        unrelated_scan = add_scan(
            session,
            unrelated,
            started_at=NOW,
            remote_ip="198.51.100.9",
        )
        sync_observations(session, selected_scan, inbound_scan, unrelated_scan)
        session.commit()
        latest = {
            selected.id: selected_scan,
            inbound.id: inbound_scan,
            unrelated.id: unrelated_scan,
        }

        rows = aggregate_current_connections(
            session,
            latest,
            source_device_ids={selected.id},
            inbound_addresses={selected.host},
        )

        assert {
            (row["source_device_id"], row["remote_ip"])
            for row in rows
        } == {
            (selected.id, selected.host),
            (inbound.id, selected.host),
        }


def test_current_sql_matches_existing_python_aggregation(app):
    with app.state.session_factory() as session:
        device = add_device(session, app, "current-equivalent", "10.0.0.40")
        current = add_scan(
            session,
            device,
            started_at=NOW,
            remote_ip="::ffff:203.0.113.8",
        )
        sync_observations(session, current)
        session.commit()
        current_id = current.id
        device_id = device.id
        session.expunge_all()
        reloaded = session.get(ScanRun, current_id)

        expected = aggregate_service_connections([reloaded], {current_id})
        actual = aggregate_current_connections(session, {device_id: reloaded})

        assert service_projection(actual) == service_projection(expected)


def test_aggregate_service_connections_ignores_local_port_and_pid(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        historical = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=8),
            local_port=50000,
            pid=100,
            remote_ip="::ffff:203.0.113.8",
        )
        current = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            local_port=50123,
            pid=200,
            remote_ip="203.0.113.8",
        )
        session.commit()

        rows = aggregate_service_connections([historical, current], {current.id})

        assert len(rows) == 1
        assert rows[0]["remote_ip"] == "203.0.113.8"
        assert rows[0]["is_current"] is True
        assert rows[0]["observation_count"] == 2
        assert rows[0]["observed_local_ports"] == [50000, 50123]
        assert rows[0]["observed_pids"] == [100, 200]
        assert rows[0]["first_seen"] == historical.started_at.isoformat()
        assert rows[0]["last_seen"] == current.started_at.isoformat()


def test_aggregate_service_connections_separates_process_names(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        first = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=2),
            process_name="curl",
        )
        second = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            process_name="wget",
        )
        session.commit()

        rows = aggregate_service_connections([first, second], {second.id})

        assert {row["process_name"] for row in rows} == {"curl", "wget"}
        assert {row["process_name"]: row["is_current"] for row in rows} == {
            "curl": False,
            "wget": True,
        }


def test_aggregate_service_connections_counts_each_scan_once(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        scan = add_scan(session, device, started_at=NOW)
        scan.connections.append(
            ConnectionRecord(
                protocol="tcp",
                address_family="ipv4",
                local_ip=device.host,
                local_port=50001,
                remote_ip="203.0.113.8",
                remote_port=443,
                state="ESTABLISHED",
                pid=101,
                process_name="client",
            )
        )
        session.commit()

        rows = aggregate_service_connections([scan], {scan.id})

        assert len(rows) == 1
        assert rows[0]["observation_count"] == 1
        assert rows[0]["observed_local_ports"] == [50000, 50001]


def test_sql_history_keeps_group_when_sample_record_purged(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        disappeared = add_scan(
            session,
            device,
            started_at=NOW - timedelta(days=5),
        )
        sync_observations(session, disappeared)
        sample_id = disappeared.connections[0].id
        # Simulate raw-record retention removing the sample row while the
        # observation stays inside the history window.
        session.execute(
            delete(ConnectionRecord).where(
                ConnectionRecord.scan_run_id == disappeared.id
            )
        )
        session.commit()

        rows = aggregate_historical_connections(
            session,
            [device.id],
            set(),
            "7d",
            now=NOW,
        )

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == sample_id
        assert row["protocol"] == "tcp"
        assert row["remote_ip"] == "203.0.113.8"
        assert row["remote_port"] == 443
        assert row["process_name"] == "client"
        assert row["state"] is None
        assert row["local_ip"] == "10.0.0.10"
        assert row["local_port"] == 50000
        assert row["pid"] == 100
        assert row["scan_id"] == disappeared.id
        assert row["is_current"] is False
        assert row["observation_count"] == 1
        assert datetime.fromisoformat(row["first_seen"]) == (
            NOW - timedelta(days=5)
        )
        assert datetime.fromisoformat(row["scan_time"]) == (
            NOW - timedelta(days=5)
        )


def test_aggregate_service_connections_hides_loopback_and_listeners(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        scan = add_scan(
            session,
            device,
            started_at=NOW,
            remote_ip="127.0.0.1",
        )
        scan.connections.append(
            ConnectionRecord(
                protocol="tcp",
                address_family="ipv4",
                local_ip="0.0.0.0",
                local_port=22,
                remote_ip=None,
                remote_port=None,
                state="LISTEN",
                pid=10,
                process_name="sshd",
            )
        )
        session.commit()

        assert aggregate_service_connections([scan], {scan.id}) == []


def test_sql_history_matches_python_aggregation(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        historical = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=8),
            local_port=50000,
            pid=100,
            remote_ip="::ffff:203.0.113.8",
        )
        current = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            local_port=50123,
            pid=200,
            remote_ip="203.0.113.8",
        )
        current.connections.append(
            ConnectionRecord(
                protocol="tcp",
                address_family="ipv4",
                local_ip=device.host,
                local_port=50124,
                remote_ip="203.0.113.8",
                remote_port=443,
                state="ESTABLISHED",
                pid=201,
                process_name="client",
            )
        )
        sync_observations(session, historical, current)
        session.commit()
        device_id = device.id
        current_id = current.id
        session.expunge_all()
        reloaded_scans = session.scalars(
            select(ScanRun)
            .where(
                ScanRun.device_id == device_id,
                ScanRun.status == ScanStatus.SUCCESS,
                ScanRun.started_at >= NOW - WINDOW_DELTAS["1d"],
            )
            .options(selectinload(ScanRun.connections))
            .order_by(ScanRun.started_at, ScanRun.id)
        ).all()

        expected = aggregate_service_connections(
            reloaded_scans,
            {current_id},
        )
        actual = aggregate_historical_connections(
            session,
            [device_id],
            {current_id},
            "1d",
            now=NOW,
        )

        assert service_projection(actual) == service_projection(expected)
        assert actual[0]["observation_count"] == 2
        assert actual[0]["observed_local_ports"] == [50000, 50123, 50124]
        assert actual[0]["observed_pids"] == [100, 200, 201]


def test_sql_history_keeps_latest_row_and_latest_non_null_hostname(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        older = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=2),
            local_port=50000,
        )
        older.connections[0].remote_hostname = "peer.internal"
        latest = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            local_port=50100,
            pid=200,
        )
        latest.connections.append(
            ConnectionRecord(
                protocol="tcp",
                address_family="ipv4",
                local_ip=device.host,
                local_port=50101,
                remote_ip="203.0.113.8",
                remote_port=443,
                state="CLOSE_WAIT",
                pid=201,
                process_name="client",
            )
        )
        sync_observations(session, older, latest)
        session.commit()

        rows = aggregate_historical_connections(
            session,
            [device.id],
            {latest.id},
            "1d",
            now=NOW,
        )

        assert len(rows) == 1
        assert rows[0]["id"] == latest.connections[-1].id
        assert rows[0]["local_port"] == 50101
        assert rows[0]["state"] == "CLOSE_WAIT"
        assert rows[0]["remote_hostname"] == "peer.internal"


def test_sql_history_filters_candidates_and_ignores_loopback(app):
    with app.state.session_factory() as session:
        selected = add_device(session, app, "selected", "10.0.0.10")
        inbound = add_device(session, app, "inbound", "10.0.0.20")
        unrelated = add_device(session, app, "unrelated", "10.0.0.30")
        selected_scan = add_scan(
            session,
            selected,
            started_at=NOW,
            remote_ip="203.0.113.8",
        )
        inbound_scan = add_scan(
            session,
            inbound,
            started_at=NOW,
            remote_ip=selected.host,
        )
        unrelated_scan = add_scan(
            session,
            unrelated,
            started_at=NOW,
            remote_ip="198.51.100.9",
        )
        selected_scan.connections.append(
            ConnectionRecord(
                protocol="tcp",
                address_family="ipv4",
                local_ip=selected.host,
                local_port=40000,
                remote_ip="127.0.0.1",
                remote_port=80,
                state="ESTABLISHED",
                pid=1,
                process_name="loop",
            )
        )
        sync_observations(session, selected_scan, inbound_scan, unrelated_scan)
        session.commit()

        actual = aggregate_historical_connections(
            session,
            [selected.id, inbound.id, unrelated.id],
            {selected_scan.id, inbound_scan.id, unrelated_scan.id},
            "1d",
            now=NOW,
            source_device_ids={selected.id},
            inbound_addresses={selected.host},
        )

        assert {
            (row["source_device_id"], row["remote_ip"])
            for row in actual
        } == {
            (selected.id, "203.0.113.8"),
            (inbound.id, selected.host),
        }


def test_sql_history_union_deduplicates_connection_matching_both_paths(app):
    with app.state.session_factory() as session:
        selected = add_device(session, app, "selected-overlap", "10.0.0.50")
        current = add_scan(
            session,
            selected,
            started_at=NOW,
            remote_ip=selected.host,
        )
        sync_observations(session, current)
        session.commit()

        rows = aggregate_historical_connections(
            session,
            [selected.id],
            {current.id},
            "1d",
            now=NOW,
            source_device_ids={selected.id},
            inbound_addresses={selected.host},
        )

        assert len(rows) == 1
        assert rows[0]["observation_count"] == 1
