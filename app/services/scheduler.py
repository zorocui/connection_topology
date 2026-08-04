import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.models import Device, ScanRun, ScanTrigger, SystemSetting
from app.services.retention import resolve_device_retention
from app.services.scan_queue import (
    PRIORITY_SCHEDULED,
    DeviceCollectionDisabled,
    ScanQueueFull,
    ScanQueueService,
)
from app.services.sqlite_writes import SQLiteWriteCoordinator

logger = logging.getLogger(__name__)


def purge_expired_scans(
    session: Session,
    system_days: int,
    *,
    now: datetime | None = None,
) -> int:
    reference = now or datetime.now(timezone.utc)
    devices = session.scalars(
        select(Device).options(selectinload(Device.cluster))
    ).all()
    groups: dict[int, list[int]] = defaultdict(list)
    for device in devices:
        policy = resolve_device_retention(device, system_days)
        groups[policy.days].append(device.id)

    deleted = 0
    for retention_days, device_ids in groups.items():
        cutoff = reference - timedelta(days=retention_days)
        result = session.execute(
            delete(ScanRun).where(
                ScanRun.device_id.in_(device_ids),
                ScanRun.started_at < cutoff,
            )
        )
        deleted += result.rowcount or 0
    session.commit()
    return deleted


class SchedulerService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        scan_queue: ScanQueueService,
        scan_jitter_seconds: int,
        write_coordinator: SQLiteWriteCoordinator | None = None,
        on_history_purged: Callable[[], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.scan_queue = scan_queue
        self.scan_jitter_seconds = scan_jitter_seconds
        self.write_coordinator = write_coordinator or SQLiteWriteCoordinator(
            session_factory,
            (0.1, 0.3),
        )
        self.on_history_purged = on_history_purged
        self.scheduler = BackgroundScheduler(timezone="UTC")

    def _enqueue_device(self, device_id: int) -> None:
        try:
            self.scan_queue.enqueue_device(
                device_id,
                ScanTrigger.SCHEDULED,
                PRIORITY_SCHEDULED,
            )
        except ScanQueueFull:
            logger.warning("扫描队列已满，跳过设备 %s 的本次定时任务", device_id)
        except DeviceCollectionDisabled:
            logger.info("设备 %s 仅用于集群标注，跳过定时采集", device_id)

    def _purge_history(self) -> None:
        with self.write_coordinator.write_once("purge_history"):
            with self.session_factory() as session:
                setting = session.get(SystemSetting, 1)
                purge_expired_scans(
                    session,
                    setting.history_retention_days if setting else 7,
                )
        if self.on_history_purged is not None:
            self.on_history_purged()

    def sync_device(self, device: Device) -> None:
        job_id = f"device-scan-{device.id}"
        if device.collection_enabled is False or not device.scheduled_enabled:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            return
        self.scheduler.add_job(
            self._enqueue_device,
            "interval",
            minutes=device.scan_interval_minutes,
            jitter=self.scan_jitter_seconds,
            args=[device.id],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    def remove_device(self, device_id: int) -> None:
        job_id = f"device-scan-{device_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def start(self) -> None:
        if self.scheduler.running:
            return
        self.scheduler.add_job(
            self._purge_history,
            "interval",
            days=1,
            id="history-retention",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        with self.session_factory() as session:
            devices = session.scalars(
                select(Device).where(
                    Device.scheduled_enabled.is_(True),
                    Device.collection_enabled.is_(True),
                )
            ).all()
            for device in devices:
                self.sync_device(device)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
