from __future__ import annotations

import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

from sqlalchemy.orm import Session, sessionmaker

from app.collectors import CollectorError, LinuxCollector, WindowsCollector
from app.collectors.base import Collector, DeviceConnectionSpec, NormalizedConnection
from app.models import ConnectionRecord, Device, OSType, ScanRun, ScanStatus, ScanTrigger
from app.security import CredentialCipher, safe_error_message

logger = logging.getLogger(__name__)


class ScanAlreadyRunning(RuntimeError):
    pass


class DeviceNotFound(RuntimeError):
    pass


class CollectionDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanTarget:
    device_id: int
    os_type: OSType
    host: str
    port: int
    username: str
    encrypted_password: str


@dataclass(frozen=True)
class ScanOutcome:
    device_id: int
    trigger: ScanTrigger
    status: ScanStatus
    started_at: datetime
    finished_at: datetime
    connections: tuple[NormalizedConnection, ...] = ()
    warning_message: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def add_scan_outcome(session: Session, outcome: ScanOutcome) -> ScanRun:
    device = session.get(Device, outcome.device_id)
    if device is None:
        raise DeviceNotFound(f"设备 {outcome.device_id} 不存在")
    run = ScanRun(
        device_id=device.id,
        trigger_type=outcome.trigger,
        status=outcome.status,
        started_at=outcome.started_at,
        finished_at=outcome.finished_at,
        connection_count=len(outcome.connections),
        warning_message=outcome.warning_message,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )
    session.add(run)
    session.flush()
    session.add_all(
        [
            ConnectionRecord(
                scan_run_id=run.id,
                protocol=row.protocol,
                address_family=row.address_family,
                local_ip=row.local_ip,
                local_port=row.local_port,
                remote_ip=row.remote_ip,
                remote_port=row.remote_port,
                state=row.state,
                pid=row.pid,
                process_name=row.process_name,
            )
            for row in outcome.connections
        ]
    )
    device.last_scan_status = outcome.status
    device.last_scan_at = outcome.finished_at
    return run


class ScanService:
    _locks_guard = threading.Lock()
    _locks: ClassVar[defaultdict[int, threading.Lock]] = defaultdict(threading.Lock)

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cipher: CredentialCipher,
        linux_collector: Collector | None = None,
        windows_collector: Collector | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.collectors: dict[OSType, Collector] = {
            OSType.LINUX: linux_collector or LinuxCollector(),
            OSType.WINDOWS: windows_collector or WindowsCollector(),
        }

    @classmethod
    def _lock_for(cls, device_id: int) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks[device_id]

    @contextmanager
    def lock_for(self, device_id: int):
        lock = self._lock_for(device_id)
        if not lock.acquire(blocking=False):
            raise ScanAlreadyRunning("该设备已有采集任务正在运行")
        try:
            yield
        finally:
            lock.release()

    def test_connection(
        self,
        os_type: OSType,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> None:
        self.collectors[os_type].test_connection(
            DeviceConnectionSpec(host=host, port=port, username=username), password
        )

    def _load_target(self, device_id: int) -> ScanTarget:
        with self.session_factory() as session:
            device = session.get(Device, device_id)
            if device is None:
                raise DeviceNotFound(f"设备 {device_id} 不存在")
            if not device.collection_enabled:
                raise CollectionDisabled("该设备仅用于集群标注，未配置采集凭据")
            return ScanTarget(
                device_id=device.id,
                os_type=device.os_type,
                host=device.host,
                port=device.port,
                username=device.username,
                encrypted_password=device.encrypted_password,
            )

    def collect(self, device_id: int, trigger: ScanTrigger) -> ScanOutcome:
        with self.lock_for(device_id):
            target = self._load_target(device_id)
            started_at = datetime.now(timezone.utc)
            password = ""
            try:
                password = self.cipher.decrypt(target.encrypted_password)
                spec = DeviceConnectionSpec(
                    host=target.host,
                    port=target.port,
                    username=target.username,
                    device_id=target.device_id,
                )
                result = self.collectors[target.os_type].collect(spec, password)
                return ScanOutcome(
                    device_id=device_id,
                    trigger=trigger,
                    status=ScanStatus.SUCCESS,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    connections=tuple(result.connections),
                    warning_message=result.warning,
                )
            except CollectorError as exc:
                message = safe_error_message(str(exc), (password,))
                logger.warning(
                    "设备 %s 采集失败 code=%s message=%s",
                    device_id,
                    exc.code,
                    message,
                )
                return ScanOutcome(
                    device_id=device_id,
                    trigger=trigger,
                    status=ScanStatus.FAILED,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    error_code=exc.code,
                    error_message=message,
                )
            except Exception as exc:  # noqa: BLE001 - worker isolation needs an outcome
                message = safe_error_message(str(exc), (password,))
                logger.exception(
                    "设备 %s 采集发生内部错误，详情已写入脱敏批次记录",
                    device_id,
                )
                return ScanOutcome(
                    device_id=device_id,
                    trigger=trigger,
                    status=ScanStatus.FAILED,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    error_code="internal_error",
                    error_message=message,
                )
