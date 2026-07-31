from pathlib import Path

from app.collectors.linux import parse_ss_output
from app.collectors.windows import parse_windows_json

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_ss_tcp_process_and_listener():
    rows = parse_ss_output((FIXTURES / "linux_ss.txt").read_text(encoding="utf-8"))
    established = next(row for row in rows if row.remote_port == 443)
    assert established.process_name == "curl"
    assert established.pid == 912
    listener = next(row for row in rows if row.local_port == 22)
    assert listener.remote_ip is None
    ipv6_udp = next(row for row in rows if row.local_port == 5353)
    assert ipv6_udp.address_family == "ipv6"


def test_parse_windows_tcp_and_udp():
    rows = parse_windows_json(
        (FIXTURES / "windows_connections.json").read_text(encoding="utf-8")
    )
    tcp = next(row for row in rows if row.protocol == "tcp")
    assert tcp.state == "ESTABLISHED"
    assert tcp.process_name == "sqlservr"
    udp = next(row for row in rows if row.protocol == "udp")
    assert udp.remote_ip is None
    assert udp.pid is None


def test_linux_collector_normalizes_ipv4_mapped_addresses():
    rows = parse_ss_output(
        "tcp ESTAB 0 0 [::ffff:10.160.79.20]:50124 "
        "[::ffff:10.160.79.21]:443"
    )

    assert rows[0].local_ip == "10.160.79.20"
    assert rows[0].remote_ip == "10.160.79.21"
    assert rows[0].address_family == "ipv4"


def test_windows_collector_normalizes_ipv4_mapped_addresses():
    rows = parse_windows_json(
        '[{"Protocol":"tcp","LocalAddress":"::ffff:10.160.79.20",'
        '"LocalPort":50124,"RemoteAddress":"::ffff:10.160.79.21",'
        '"RemotePort":443,"State":"Established","OwningProcess":12}]'
    )

    assert rows[0].local_ip == "10.160.79.20"
    assert rows[0].remote_ip == "10.160.79.21"
    assert rows[0].address_family == "ipv4"
