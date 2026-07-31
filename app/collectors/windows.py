from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

try:
    import winrm
    from winrm.exceptions import (
        InvalidCredentialsError,
        WinRMOperationTimeoutError,
        WinRMTransportError,
    )
except ModuleNotFoundError as exc:
    if exc.name != "winrm" and not (exc.name or "").startswith("winrm."):
        raise
    winrm = None
    InvalidCredentialsError = ()
    WinRMOperationTimeoutError = ()
    WinRMTransportError = ()

from app.collectors.base import (
    CollectionResult,
    CollectorError,
    DeviceConnectionSpec,
    NormalizedConnection,
    address_family,
    normalize_ip_address,
)

WINDOWS_CONNECTION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$tcp = Get-NetTCPConnection | ForEach-Object {
  $processName = $null
  try { $processName = (Get-Process -Id $_.OwningProcess -ErrorAction Stop).ProcessName } catch {}
  [PSCustomObject]@{
    Protocol='tcp'; LocalAddress=$_.LocalAddress; LocalPort=$_.LocalPort;
    RemoteAddress=$_.RemoteAddress; RemotePort=$_.RemotePort; State=[string]$_.State;
    OwningProcess=$_.OwningProcess; ProcessName=$processName
  }
}
$udp = Get-NetUDPEndpoint | ForEach-Object {
  $processName = $null
  try { $processName = (Get-Process -Id $_.OwningProcess -ErrorAction Stop).ProcessName } catch {}
  [PSCustomObject]@{
    Protocol='udp'; LocalAddress=$_.LocalAddress; LocalPort=$_.LocalPort;
    RemoteAddress=$null; RemotePort=$null; State=$null;
    OwningProcess=$_.OwningProcess; ProcessName=$processName
  }
}
@($tcp) + @($udp) | ConvertTo-Json -Compress
""".strip()

WINDOWS_TEST_SCRIPT = (
    "Get-Command Get-NetTCPConnection,Get-NetUDPEndpoint -ErrorAction Stop | "
    "Select-Object -ExpandProperty Name | ConvertTo-Json -Compress"
)
WINDOWS_COMPONENT_ERROR_CODE = "windows_component_unavailable"
WINDOWS_COMPONENT_ERROR_MESSAGE = "当前环境未安装 Windows 采集组件，请安装 pywinrm"


def _value(row: dict, *names: str):
    for name in names:
        if name in row:
            return row[name]
    return None


def parse_windows_json(output: str) -> tuple[NormalizedConnection, ...]:
    if not output.strip():
        return ()
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CollectorError("parse_error", "Windows 返回了无效 JSON") from exc
    rows = decoded if isinstance(decoded, list) else [decoded]
    result: list[NormalizedConnection] = []
    for row in rows:
        protocol = str(_value(row, "Protocol", "protocol")).lower()
        if protocol not in {"tcp", "udp"}:
            continue
        local_ip = str(_value(row, "LocalAddress", "local_address"))
        local_port = int(_value(row, "LocalPort", "local_port"))
        remote_raw = _value(row, "RemoteAddress", "remote_address")
        remote_port_raw = _value(row, "RemotePort", "remote_port")
        remote_ip = None if remote_raw in {None, "", "0.0.0.0", "::"} else str(remote_raw)
        local_ip = normalize_ip_address(local_ip)
        remote_ip = normalize_ip_address(remote_ip)
        assert local_ip is not None
        remote_port = (
            None if remote_ip is None or remote_port_raw in {None, 0, "0"} else int(remote_port_raw)
        )
        state_raw = _value(row, "State", "state")
        pid_raw = _value(row, "OwningProcess", "pid")
        process_raw = _value(row, "ProcessName", "process_name")
        result.append(
            NormalizedConnection(
                protocol=protocol,
                address_family=address_family(local_ip),
                local_ip=local_ip,
                local_port=local_port,
                remote_ip=remote_ip,
                remote_port=remote_port,
                state=str(state_raw).upper() if state_raw else None,
                pid=int(pid_raw) if pid_raw not in {None, 0, "0"} else None,
                process_name=str(process_raw) if process_raw else None,
            )
        )
    return tuple(result)


class WindowsCollector:
    def __init__(
        self,
        timeout: int = 15,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.timeout = timeout
        self.session_factory = session_factory

    def _session(self, device: DeviceConnectionSpec, password: str) -> Any:
        session_factory = self.session_factory
        if session_factory is None:
            if winrm is None:
                raise CollectorError(
                    WINDOWS_COMPONENT_ERROR_CODE,
                    WINDOWS_COMPONENT_ERROR_MESSAGE,
                )
            session_factory = winrm.Session
        scheme = "https" if device.port == 5986 else "http"
        endpoint = f"{scheme}://{device.host}:{device.port}/wsman"
        return session_factory(
            endpoint,
            auth=(device.username, password),
            transport="ntlm",
            read_timeout_sec=self.timeout + 5,
            operation_timeout_sec=self.timeout,
            server_cert_validation="ignore" if scheme == "https" else "validate",
        )

    def _run(self, session: Any, script: str) -> str:
        try:
            response = session.run_ps(script)
        except InvalidCredentialsError as exc:
            raise CollectorError("authentication_failed", "WinRM 用户名或密码错误") from exc
        except WinRMOperationTimeoutError as exc:
            raise CollectorError("command_timeout", "WinRM 命令执行超时") from exc
        except WinRMTransportError as exc:
            raise CollectorError("connection_failed", "无法连接 WinRM 服务") from exc
        except Exception as exc:
            raise CollectorError("connection_failed", f"WinRM 连接失败: {exc}") from exc
        stdout = response.std_out.decode("utf-8-sig", errors="replace")
        stderr = response.std_err.decode("utf-8-sig", errors="replace").strip()
        if response.status_code != 0:
            raise CollectorError("command_failed", stderr or "Windows 采集命令执行失败")
        return stdout

    def test_connection(self, device: DeviceConnectionSpec, password: str) -> None:
        self._run(self._session(device, password), WINDOWS_TEST_SCRIPT)

    def collect(self, device: DeviceConnectionSpec, password: str) -> CollectionResult:
        output = self._run(self._session(device, password), WINDOWS_CONNECTION_SCRIPT)
        return CollectionResult(parse_windows_json(output))
