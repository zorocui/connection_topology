from datetime import datetime, timezone

import pytest

from app.models import (
    ConnectionRecord,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.topology import HostAddressResolver, build_topology, diff_scans


def make_scan(scan_id: int, remote_ip: str) -> ScanRun:
    device = Device(
        id=1,
        name="server-1",
        host="10.160.79.20",
        os_type=OSType.LINUX,
        port=22,
        username="tester",
        encrypted_password="secret",
    )
    connection = ConnectionRecord(
        id=scan_id,
        protocol="tcp",
        address_family="ipv6",
        local_ip="::ffff:10.160.79.20",
        local_port=50000,
        remote_ip=remote_ip,
        remote_port=443,
        state="ESTABLISHED",
        pid=100,
        process_name="client",
    )
    scan = ScanRun(
        id=scan_id,
        device=device,
        trigger_type=ScanTrigger.MANUAL,
        status=ScanStatus.SUCCESS,
        started_at=datetime.now(timezone.utc),
        connection_count=1,
    )
    scan.connections = [connection]
    return scan


def test_topology_merges_mapped_and_plain_ipv4_peers():
    scan = make_scan(1, "::ffff:10.160.79.21")
    second = ConnectionRecord(
        id=2,
        protocol="tcp",
        address_family="ipv4",
        local_ip="10.160.79.20",
        local_port=50001,
        remote_ip="10.160.79.21",
        remote_port=80,
        state="ESTABLISHED",
    )
    scan.connections.append(second)
    scan.connection_count = 2

    topology = build_topology(scan)

    assert len(topology["edges"]) == 1
    assert topology["edges"][0]["data"]["count"] == 2
    details = topology["edges"][0]["data"]["connections"]
    assert {row["remote_ip"] for row in details} == {"10.160.79.21"}
    assert {row["local_ip"] for row in details} == {"10.160.79.20"}


def test_scan_diff_treats_mapped_and_plain_ipv4_as_equal():
    previous = make_scan(1, "::ffff:10.160.79.21")
    current = make_scan(2, "10.160.79.21")

    result = diff_scans(previous, current)

    assert result["added"] == []
    assert result["removed"] == []


def test_host_resolver_normalizes_mapped_ipv4():
    resolver = HostAddressResolver()

    assert resolver.resolve("::ffff:10.160.79.21") == {"10.160.79.21"}


@pytest.mark.parametrize(
    "remote_ip",
    ["127.0.0.1", "127.23.45.67", "::1", "::ffff:127.0.0.1"],
)
def test_device_topology_hides_loopback_connections_without_mutating_scan(remote_ip):
    scan = make_scan(1, remote_ip)

    topology = build_topology(scan)

    assert topology["edges"] == []
    assert len(topology["nodes"]) == 1
    assert len(scan.connections) == 1
    assert scan.connection_count == 1


def test_device_topology_keeps_native_external_ipv6():
    scan = make_scan(1, "2001:db8::1")

    topology = build_topology(scan)

    assert len(topology["edges"]) == 1
    assert topology["nodes"][1]["data"]["subtitle"] == "2001:db8::1"


def test_device_history_marks_disconnected_and_mixed_edges():
    historical = make_scan(10, "203.0.113.10")
    historical.started_at = datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc)
    historical.connections.append(
        ConnectionRecord(
            id=11,
            protocol="tcp",
            address_family="ipv4",
            local_ip="10.160.79.20",
            local_port=51000,
            remote_ip="203.0.113.20",
            remote_port=443,
            state="ESTABLISHED",
            pid=101,
            process_name="client",
        )
    )
    current = make_scan(20, "203.0.113.20")
    current.started_at = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)
    current.device = historical.device

    topology = build_topology(current, scans=[historical, current], window="1d")
    edges = {
        edge["data"]["connections"][0]["remote_ip"]: edge["data"]
        for edge in topology["edges"]
    }

    assert topology["window"] == "1d"
    assert edges["203.0.113.10"]["is_current"] is False
    assert edges["203.0.113.10"]["historical_count"] == 1
    assert edges["203.0.113.20"]["is_current"] is True
    assert edges["203.0.113.20"]["current_count"] == 1


def test_device_history_merges_service_reconnects():
    historical = make_scan(30, "203.0.113.30")
    historical.connections[0].local_port = 50000
    historical.connections[0].pid = 100
    current = make_scan(31, "203.0.113.30")
    current.device = historical.device
    current.connections[0].local_port = 50100
    current.connections[0].pid = 200

    topology = build_topology(current, scans=[historical, current], window="1d")
    detail = topology["edges"][0]["data"]["connections"][0]

    assert topology["edges"][0]["data"]["count"] == 1
    assert detail["observation_count"] == 2
    assert detail["observed_local_ports"] == [50000, 50100]
    assert detail["observed_pids"] == [100, 200]
