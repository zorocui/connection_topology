from __future__ import annotations

import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import ClassVar

from sqlalchemy.orm import Session

from app.collectors import CollectorError, LinuxCollector, WindowsCollector
from app.collectors.base import Collector, DeviceConnectionSpec
from app.models import ConnectionRecord, Device, OSType, ScanRun, ScanStatus, ScanTrigger
from app.security import CredentialCipher, safe_error_message

logger = logging.getLogger(__name__)


class ScanAlreadyRunning(RuntimeError):
    pass


class DeviceNotFound(RuntimeError):
    pass


class ScanService:
    _locks_guard = threading.Lock()
    _locks: ClassVar[defaultdict[int, threading.Lock]] = defaultdict(threading.Lock)

    def __init__(
        self,
        session: Session,
        cipher: CredentialCipher,
        linux_collector: Collector | None = None,
        windows_collector: Collector | None = None,
    ) -> None:
        self.session = session
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

    def run(self, device_id: int, trigger: ScanTrigger) -> ScanRun:
        with self.lock_for(device_id):
            device = self.session.get(Device, device_id)
            if device is None:
                raise DeviceNotFound(f"设备 {device_id} 不存在")
            run = ScanRun(
                device_id=device.id,
                trigger_type=trigger,
                status=ScanStatus.RUNNING,
                started_at=datetime.now(timezone.utc),
            )
            self.session.add(run)
            self.session.commit()
            password = ""
            try:
                password = self.cipher.decrypt(device.encrypted_password)
                spec = DeviceConnectionSpec(
                    host=device.host,
                    port=device.port,
                    username=device.username,
                )
                result = self.collectors[device.os_type].collect(spec, password)
                records = [
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
                    for row in result.connections
                ]
                self.session.add_all(records)
                finished_at = datetime.now(timezone.utc)
                run.status = ScanStatus.SUCCESS
                run.finished_at = finished_at
                run.connection_count = len(records)
                run.warning_message = result.warning
                device.last_scan_status = ScanStatus.SUCCESS
                device.last_scan_at = finished_at
                self.session.commit()
            except CollectorError as exc:
                self.session.rollback()
                run = self.session.get(ScanRun, run.id)
                device = self.session.get(Device, device_id)
                assert run is not None and device is not None
                run.status = ScanStatus.FAILED
                run.finished_at = datetime.now(timezone.utc)
                run.error_code = exc.code
                run.error_message = safe_error_message(str(exc), (password,))
                device.last_scan_status = ScanStatus.FAILED
                device.last_scan_at = run.finished_at
                self.session.commit()
                logger.warning(
                    "设备 %s 采集失败 code=%s message=%s",
                    device_id,
                    exc.code,
                    run.error_message,
                )
            except Exception as exc:  # noqa: BLE001 - task isolation requires a failed run record
                self.session.rollback()
                run = self.session.get(ScanRun, run.id)
                device = self.session.get(Device, device_id)
                assert run is not None and device is not None
                run.status = ScanStatus.FAILED
                run.finished_at = datetime.now(timezone.utc)
                run.error_code = "internal_error"
                run.error_message = safe_error_message(str(exc), (password,))
                device.last_scan_status = ScanStatus.FAILED
                device.last_scan_at = run.finished_at
                self.session.commit()
                logger.error("设备 %s 采集发生内部错误，详情已写入脱敏批次记录", device_id)
            self.session.refresh(run)
            return run
