from pathlib import Path

import pytest

from app.collectors import linux as linux_module
from app.collectors.base import CollectorError
from app.collectors.linux import LinuxCollector, parse_ss_output
from app.collectors.windows import parse_windows_json

FIXTURES = Path(__file__).parent / "fixtures"


class FakeChannel:
    def __init__(
        self,
        stdout_chunks=(),
        stderr_chunks=(),
        *,
        never_exit=False,
    ):
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_chunks = list(stderr_chunks)
        self.never_exit = never_exit
        self.closed = False

    def recv_ready(self):
        return bool(self.stdout_chunks)

    def recv(self, _size):
        return self.stdout_chunks.pop(0)

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv_stderr(self, _size):
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self):
        return (
            not self.never_exit
            and not self.stdout_chunks
            and not self.stderr_chunks
        )

    def recv_exit_status(self):
        assert not self.never_exit
        assert not self.stdout_chunks
        assert not self.stderr_chunks
        return 0

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, channel):
        self.channel = channel

    def read(self):
        return b""


class FakeSSHClient:
    def __init__(self, channel):
        self.channel = channel

    def exec_command(self, _command, *, timeout):
        assert timeout > 0
        stream = FakeStream(self.channel)
        return None, stream, stream


def test_parse_ss_tcp_process_and_listener():
    rows = parse_ss_output((FIXTURES / "linux_ss.txt").read_text(encoding="utf-8"))
    established = next(row for row in rows if row.remote_port == 443)
    assert established.process_name == "curl"
    assert established.pid == 912
    listener = next(row for row in rows if row.local_port == 22)
    assert listener.remote_ip is None
    ipv6_udp = next(row for row in rows if row.local_port == 5353)
    assert ipv6_udp.address_family == "ipv6"


def test_parse_ss_keeps_valid_rows_when_tcp_candidate_is_short():
    rows = parse_ss_output(
        "tcp BROKEN\n"
        "tcp ESTAB 0 0 10.0.0.10:50124 10.0.0.20:443"
    )

    assert len(rows) == 1
    assert rows[0].remote_port == 443


def test_parse_ss_keeps_valid_rows_when_endpoint_is_invalid():
    rows = parse_ss_output(
        "udp UNCONN 0 0 invalid-endpoint 10.0.0.20:53\n"
        "udp UNCONN 0 0 10.0.0.10:5353 10.0.0.20:53"
    )

    assert len(rows) == 1
    assert rows[0].local_port == 5353


def test_parse_ss_ignores_non_connection_text_and_empty_output():
    assert parse_ss_output("") == ()
    assert parse_ss_output("diagnostic text") == ()


def test_parse_ss_fails_when_all_tcp_udp_candidates_are_invalid():
    with pytest.raises(CollectorError, match="无法解析 ss 第 2 行") as captured:
        parse_ss_output("diagnostic text\nudp BROKEN")

    assert captured.value.code == "parse_error"


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


def test_linux_execute_drains_output_before_waiting_for_exit_status():
    channel = FakeChannel(
        (b"part-1", b"part-2"),
        (b"warning",),
    )
    collector = LinuxCollector(timeout=1)

    code, output, error = collector._execute(
        FakeSSHClient(channel),
        "ss -H -tuna",
    )

    assert code == 0
    assert output == "part-1part-2"
    assert error == "warning"


def test_linux_execute_closes_channel_on_total_timeout(monkeypatch):
    channel = FakeChannel(never_exit=True)
    collector = LinuxCollector(timeout=1)
    monotonic_values = iter((0.0, 2.0))
    monkeypatch.setattr(linux_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(linux_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(CollectorError) as captured:
        collector._execute(FakeSSHClient(channel), "ss -H -tuna")

    assert captured.value.code == "command_timeout"
    assert str(captured.value) == "远程 ss 命令执行超时"
    assert channel.closed is True
