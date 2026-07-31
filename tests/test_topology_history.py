from datetime import datetime, timedelta, timezone

from app.models import (
    ConnectionRecord,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.topology_history import (
    aggregate_historical_connections,
    aggregate_service_connections,
    load_current_scans,
    load_topology_scans,
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


def test_load_topology_scans_uses_window_and_success_only(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        old = add_scan(session, device, started_at=NOW - timedelta(days=2))
        recent = add_scan(session, device, started_at=NOW - timedelta(hours=12))
        add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            status=ScanStatus.FAILED,
        )
        session.commit()

        current, scans = load_topology_scans(session, [device.id], "1d", now=NOW)

        assert current[device.id].id == recent.id
        assert [scan.id for scan in scans] == [recent.id]
        assert old.id not in {scan.id for scan in scans}


def test_load_topology_scans_keeps_old_current_baseline(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        old_current = add_scan(session, device, started_at=NOW - timedelta(days=10))
        session.commit()

        current, scans = load_topology_scans(session, [device.id], "7d", now=NOW)

        assert current[device.id].id == old_current.id
        assert [scan.id for scan in scans] == [old_current.id]


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
        add_scan(
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
        session.commit()
        device_id = device.id
        current_id = current.id
        session.expunge_all()
        _, reloaded_scans = load_topology_scans(
            session,
            [device_id],
            "1d",
            now=NOW,
        )

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
