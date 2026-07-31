import re
import time
from collections.abc import Callable

import paramiko

from app.collectors.base import (
    CollectionResult,
    CollectorError,
    DeviceConnectionSpec,
    NormalizedConnection,
    address_family,
    normalize_ip_address,
)

SS_WITH_PROCESS = "ss -H -tunap"
SS_WITHOUT_PROCESS = "ss -H -tuna"
_PROCESS_RE = re.compile(r'\(\("(?P<name>[^"]+)",pid=(?P<pid>\d+)')


def _parse_endpoint(value: str, *, remote: bool) -> tuple[str | None, int | None]:
    value = value.strip()
    if value in {"*", "*:*", "0.0.0.0:0", "[::]:0"} and remote:
        return None, None

    if value.startswith("["):
        closing = value.rfind("]")
        host = value[1:closing]
        port_text = value[closing + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            raise ValueError(f"无效端点: {value}")

    host = host.split("%", 1)[0]
    if host == "*":
        host = "0.0.0.0"
    if remote and port_text in {"*", "0"}:
        return None, None
    port = 0 if port_text == "*" else int(port_text)
    return host, port


def parse_ss_output(output: str) -> tuple[NormalizedConnection, ...]:
    rows: list[NormalizedConnection] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=6)
        if len(parts) < 6:
            raise CollectorError("parse_error", f"无法解析 ss 第 {line_number} 行")
        protocol = parts[0].lower()
        if protocol.startswith("tcp"):
            protocol = "tcp"
        elif protocol.startswith("udp"):
            protocol = "udp"
        else:
            continue
        state = parts[1].upper() if protocol == "tcp" else None
        try:
            local_ip, local_port = _parse_endpoint(parts[4], remote=False)
            remote_ip, remote_port = _parse_endpoint(parts[5], remote=True)
        except (ValueError, IndexError) as exc:
            raise CollectorError("parse_error", f"无法解析 ss 第 {line_number} 行端点") from exc

        local_ip = normalize_ip_address(local_ip)
        remote_ip = normalize_ip_address(remote_ip)
        process_name = None
        pid = None
        if len(parts) == 7:
            process_match = _PROCESS_RE.search(parts[6])
            if process_match:
                process_name = process_match.group("name")
                pid = int(process_match.group("pid"))
        assert local_ip is not None and local_port is not None
        rows.append(
            NormalizedConnection(
                protocol=protocol,
                address_family=address_family(local_ip),
                local_ip=local_ip,
                local_port=local_port,
                remote_ip=remote_ip,
                remote_port=remote_port,
                state=state,
                pid=pid,
                process_name=process_name,
            )
        )
    return tuple(rows)


class LinuxCollector:
    def __init__(
        self,
        timeout: int = 15,
        client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
    ) -> None:
        self.timeout = timeout
        self.client_factory = client_factory

    def _connect(self, device: DeviceConnectionSpec, password: str) -> paramiko.SSHClient:
        client = self.client_factory()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=device.host,
                port=device.port,
                username=device.username,
                password=password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            return client
        except paramiko.AuthenticationException as exc:
            client.close()
            raise CollectorError("authentication_failed", "SSH 用户名或密码错误") from exc
        except TimeoutError as exc:
            client.close()
            raise CollectorError("connection_timeout", "SSH 连接超时") from exc
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise CollectorError("connection_failed", f"SSH 连接失败: {exc}") from exc

    def _execute(self, client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
        channel = None
        try:
            _, stdout, _ = client.exec_command(command, timeout=self.timeout)
            channel = stdout.channel
            deadline = time.monotonic() + self.timeout
            output_chunks: list[bytes] = []
            error_chunks: list[bytes] = []
            while True:
                while channel.recv_ready():
                    output_chunks.append(channel.recv(65536))
                while channel.recv_stderr_ready():
                    error_chunks.append(channel.recv_stderr(65536))
                if channel.exit_status_ready():
                    while channel.recv_ready():
                        output_chunks.append(channel.recv(65536))
                    while channel.recv_stderr_ready():
                        error_chunks.append(channel.recv_stderr(65536))
                    break
                if time.monotonic() >= deadline:
                    channel.close()
                    raise CollectorError(
                        "command_timeout",
                        "远程 ss 命令执行超时",
                    )
                time.sleep(0.01)
            return (
                channel.recv_exit_status(),
                b"".join(output_chunks).decode("utf-8", errors="replace"),
                b"".join(error_chunks).decode("utf-8", errors="replace"),
            )
        except TimeoutError as exc:
            if channel is not None:
                channel.close()
            raise CollectorError("command_timeout", "远程 ss 命令执行超时") from exc
        except paramiko.SSHException as exc:
            raise CollectorError("command_failed", "无法执行远程 ss 命令") from exc

    def test_connection(self, device: DeviceConnectionSpec, password: str) -> None:
        client = self._connect(device, password)
        try:
            code, _, error = self._execute(client, "ss -H -tuna")
            if code != 0:
                raise CollectorError("command_unavailable", error.strip() or "服务器未安装 ss")
        finally:
            client.close()

    def collect(self, device: DeviceConnectionSpec, password: str) -> CollectionResult:
        client = self._connect(device, password)
        try:
            code, output, error = self._execute(client, SS_WITH_PROCESS)
            warning = None
            if code != 0:
                fallback_code, output, fallback_error = self._execute(client, SS_WITHOUT_PROCESS)
                if fallback_code != 0:
                    detail = fallback_error.strip() or error.strip() or "服务器未安装 ss"
                    raise CollectorError("command_unavailable", detail)
                warning = "当前账户无法读取完整进程信息，已降级采集网络连接"
            return CollectionResult(parse_ss_output(output), warning)
        finally:
            client.close()
