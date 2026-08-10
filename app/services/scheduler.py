import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.models import (
    ConnectionRecord,
    Device,
    ScanRun,
    ScanStatus,
    ScanTrigger,
    SystemSetting,
)
from app.services.database_transactions import PostgresTransactionRunner
from app.services.postgres_leader import (
    SCHEDULER_LEADER_LOCK_KEY,
    PostgresLeaderElector,
)
from app.services.postgres_notifications import notify_topology_changed
from app.services.retention import resolve_device_retention
from app.services.scan_queue import (
    PRIORITY_SCHEDULED,
    DeviceCollectionDisabled,
    ScanQueueFull,
    ScanQueueService,
)

logger = logging.getLogger(__name__)

# Scan runs deleted per purge batch. At steady state a full retention window
# can cascade to tens of millions of rows, so purges commit in small chunks
# instead of one long transaction (WAL spikes, lock hold time).
PURGE_SCAN_CHUNK_SIZE = 2000

# Raw connection records are only read back for the latest successful scan
# per device (current topology) and the one before it (scan diff), so the
# raw purge always keeps those two baselines per device even when they are
# older than the cutoff. History views read connection_service_observations
# and are unaffected.
RAW_RETENTION_KEPT_SCANS_PER_DEVICE = 2


def _delete_scans_in_chunks(
    session: Session,
    conditions: list,
    chunk_size: int,
) -> int:
    deleted = 0
    while True:
        scan_ids = session.scalars(
            select(ScanRun.id)
            .where(*conditions)
            .order_by(ScanRun.id)
            .limit(chunk_size)
        ).all()
        if not scan_ids:
            return deleted
        result = session.execute(delete(ScanRun).where(ScanRun.id.in_(scan_ids)))
        session.commit()
        deleted += result.rowcount or 0


def purge_expired_scans(
    session: Session,
    system_days: int,
    *,
    now: datetime | None = None,
    chunk_size: int = PURGE_SCAN_CHUNK_SIZE,
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
        deleted += _delete_scans_in_chunks(
            session,
            [ScanRun.device_id.in_(device_ids), ScanRun.started_at < cutoff],
            chunk_size,
        )
    if deleted:
        notify_topology_changed(session)
        session.commit()
    return deleted


def purge_raw_connection_records(
    session: Session,
    raw_days: int,
    *,
    now: datetime | None = None,
    chunk_size: int = PURGE_SCAN_CHUNK_SIZE,
) -> int:
    """Delete raw connection rows older than ``raw_days`` while keeping the
    scan_runs and service observations for the full history retention.

    Returns the number of deleted connection rows. The per-device latest two
    successful scans keep their raw rows so the current topology and the
    latest diff keep working for devices that stopped reporting.
    """
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=raw_days)
    ranked = (
        select(
            ScanRun.id.label("scan_id"),
            func.row_number()
            .over(
                partition_by=ScanRun.device_id,
                order_by=(desc(ScanRun.started_at), desc(ScanRun.id)),
            )
            .label("position"),
        )
        .where(ScanRun.status == ScanStatus.SUCCESS)
        .subquery()
    )
    kept_scan_ids = select(ranked.c.scan_id).where(
        ranked.c.position <= RAW_RETENTION_KEPT_SCANS_PER_DEVICE
    )
    deleted = 0
    # Watermark on scan id: the scan rows themselves survive this purge, so a
    # plain re-select would return the same ids forever.
    last_scan_id = 0
    while True:
        scan_ids = session.scalars(
            select(ScanRun.id)
            .where(
                ScanRun.started_at < cutoff,
                ScanRun.id > last_scan_id,
                ScanRun.id.not_in(kept_scan_ids),
            )
            .order_by(ScanRun.id)
            .limit(chunk_size)
        ).all()
        if not scan_ids:
            return deleted
        result = session.execute(
            delete(ConnectionRecord).where(
                ConnectionRecord.scan_run_id.in_(scan_ids)
            )
        )
        session.commit()
        deleted += result.rowcount or 0
        last_scan_id = scan_ids[-1]


class SchedulerService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        scan_queue: ScanQueueService,
        scan_jitter_seconds: int,
        transaction_runner: PostgresTransactionRunner | None = None,
        on_history_purged: Callable[[], None] | None = None,
        raw_retention_days: int = 2,
    ) -> None:
        self.session_factory = session_factory
        self.scan_queue = scan_queue
        self.scan_jitter_seconds = scan_jitter_seconds
        self.raw_retention_days = raw_retention_days
        self.transaction_runner = transaction_runner or PostgresTransactionRunner(
            session_factory,
            (0.1, 0.3),
        )
        self.on_history_purged = on_history_purged
        self.scheduler = BackgroundScheduler(timezone="UTC")
        engine = session_factory.kw.get("bind")
        if engine is None:
            raise ValueError("scheduler session factory must be bound to an engine")
        self.elector = PostgresLeaderElector(
            engine,
            SCHEDULER_LEADER_LOCK_KEY,
            self._become_leader,
            self._lose_leadership,
        )
        self._leadership_lock = threading.RLock()

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
        with (
            self.transaction_runner.guard("purge_history"),
            self.session_factory() as session,
        ):
            setting = session.get(SystemSetting, 1)
            raw_deleted = purge_raw_connection_records(
                session,
                self.raw_retention_days,
            )
            deleted = purge_expired_scans(
                session,
                setting.history_retention_days if setting else 7,
            )
        if raw_deleted or deleted:
            logger.info(
                "历史清理完成：删除原始连接 %s 行、扫描快照 %s 个",
                raw_deleted,
                deleted,
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
        self.elector.start()

    def _become_leader(self) -> None:
        with self._leadership_lock:
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

    def _lose_leadership(self) -> None:
        with self._leadership_lock:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
                self.scheduler = BackgroundScheduler(timezone="UTC")

    def shutdown(self) -> None:
        self.elector.shutdown()
