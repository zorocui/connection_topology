from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import Collector
from app.models import (
    Device,
    ImportBatch,
    ImportRowResult,
    ImportTestStatus,
    ScanBatch,
    ScanBatchItem,
    ScanBatchStatus,
    ScanBatchType,
    ScanStatus,
    ScanTask,
    ScanTaskStatus,
    ScanTrigger,
)
from app.security import CredentialCipher
from app.services.scans import ScanService

logger = logging.getLogger(__name__)

PRIORITY_SCHEDULED = 20
PRIORITY_IMPORT = 60
PRIORITY_BATCH = 80
PRIORITY_MANUAL = 100
ACTIVE_TASK_STATUSES = (ScanTaskStatus.PENDING, ScanTaskStatus.RUNNING)
TERMINAL_TASK_STATUSES = (
    ScanTaskStatus.SUCCESS,
    ScanTaskStatus.FAILED,
    ScanTaskStatus.CANCELLED,
)


class ScanQueueFull(RuntimeError):
    pass


COLLECTION_DISABLED_MESSAGE = "该设备仅用于集群标注，未配置采集凭据"


class DeviceCollectionDisabled(RuntimeError):
    pass


class ScanQueueService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cipher: CredentialCipher,
        linux_collector: Collector,
        windows_collector: Collector,
        *,
        max_workers: int,
        queue_size: int,
        on_successful_scan: Callable[[], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.linux_collector = linux_collector
        self.windows_collector = windows_collector
        self.max_workers = max_workers
        self.queue_size = queue_size
        self.on_successful_scan = on_successful_scan
        self._enqueue_lock = threading.RLock()
        self._claim_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        self._dispatcher_thread: threading.Thread | None = None
        self._futures: set[Future] = set()
        self._futures_lock = threading.Lock()

    def _active_task(self, session: Session, device_id: int) -> ScanTask | None:
        return session.scalar(
            select(ScanTask).where(
                ScanTask.device_id == device_id,
                ScanTask.status.in_(ACTIVE_TASK_STATUSES),
            )
        )

    def _active_count(self, session: Session) -> int:
        return (
            session.scalar(
                select(func.count()).select_from(ScanTask).where(
                    ScanTask.status.in_(ACTIVE_TASK_STATUSES)
                )
            )
            or 0
        )

    def _attach_batch_item(
        self,
        session: Session,
        batch_id: int,
        task: ScanTask,
    ) -> None:
        existing = session.scalar(
            select(ScanBatchItem).where(
                ScanBatchItem.batch_id == batch_id,
                ScanBatchItem.device_id == task.device_id,
            )
        )
        if existing is None:
            session.add(
                ScanBatchItem(
                    batch_id=batch_id,
                    task_id=task.id,
                    device_id=task.device_id,
                    status=task.status,
                )
            )

    def _enqueue_in_session(
        self,
        session: Session,
        device_id: int,
        trigger: ScanTrigger,
        priority: int,
        batch_id: int | None = None,
    ) -> ScanTask:
        device = session.get(Device, device_id)
        if device is None:
            raise ValueError("设备不存在")
        if not device.collection_enabled:
            raise DeviceCollectionDisabled(COLLECTION_DISABLED_MESSAGE)
        task = self._active_task(session, device_id)
        if task is None:
            if self._active_count(session) >= self.queue_size:
                raise ScanQueueFull("扫描队列已满，请稍后重试")
            task = ScanTask(
                device_id=device_id,
                trigger_type=trigger,
                priority=priority,
                status=ScanTaskStatus.PENDING,
            )
            session.add(task)
            session.flush()
        elif priority > task.priority:
            task.priority = priority
            task.trigger_type = trigger
        if batch_id is not None:
            self._attach_batch_item(session, batch_id, task)
        return task

    def enqueue_device(
        self,
        device_id: int,
        trigger: ScanTrigger,
        priority: int,
        batch_id: int | None = None,
    ) -> ScanTask:
        with self._enqueue_lock, self.session_factory() as session:
            task = self._enqueue_in_session(
                session,
                device_id,
                trigger,
                priority,
                batch_id,
            )
            session.flush()
            if batch_id is not None:
                self._refresh_batch(session, batch_id)
            session.commit()
            session.refresh(task)
        self._wake_event.set()
        return task

    def _batch_trigger_and_priority(
        self, batch_type: ScanBatchType
    ) -> tuple[ScanTrigger, int]:
        if batch_type == ScanBatchType.IMPORT:
            return ScanTrigger.IMPORT, PRIORITY_IMPORT
        return ScanTrigger.BATCH, PRIORITY_BATCH

    def _create_batch_in_session(
        self,
        session: Session,
        batch_type: ScanBatchType,
        device_ids: Sequence[int],
        *,
        cluster_id: int | None = None,
        source_import_batch_id: int | None = None,
    ) -> ScanBatch:
        unique_ids = list(dict.fromkeys(device_ids))
        existing_devices = set(
            session.scalars(
                select(Device.id).where(
                    Device.id.in_(unique_ids),
                    Device.collection_enabled.is_(True),
                )
            ).all()
        )
        selected_ids = [device_id for device_id in unique_ids if device_id in existing_devices]
        active_device_ids = set(
            session.scalars(
                select(ScanTask.device_id).where(
                    ScanTask.device_id.in_(selected_ids),
                    ScanTask.status.in_(ACTIVE_TASK_STATUSES),
                )
            ).all()
        )
        new_task_count = len(selected_ids) - len(active_device_ids)
        if self._active_count(session) + new_task_count > self.queue_size:
            raise ScanQueueFull("扫描队列已满，请稍后重试")
        batch = ScanBatch(
            batch_type=batch_type,
            status=ScanBatchStatus.PENDING,
            cluster_id=cluster_id,
            source_import_batch_id=source_import_batch_id,
        )
        session.add(batch)
        session.flush()
        trigger, priority = self._batch_trigger_and_priority(batch_type)
        for device_id in selected_ids:
            self._enqueue_in_session(
                session,
                device_id,
                trigger,
                priority,
                batch.id,
            )
        session.flush()
        self._refresh_batch(session, batch.id)
        return batch

    def create_batch(
        self,
        batch_type: ScanBatchType,
        device_ids: Sequence[int],
        *,
        cluster_id: int | None = None,
        source_import_batch_id: int | None = None,
    ) -> ScanBatch:
        with self._enqueue_lock, self.session_factory() as session:
            batch = self._create_batch_in_session(
                session,
                batch_type,
                device_ids,
                cluster_id=cluster_id,
                source_import_batch_id=source_import_batch_id,
            )
            session.commit()
            session.refresh(batch)
        self._wake_event.set()
        return batch

    def create_import_scan_batch(self, import_batch_id: int) -> ScanBatch | None:
        with self._enqueue_lock, self.session_factory() as session:
            import_batch = session.get(ImportBatch, import_batch_id)
            if import_batch is None:
                return None
            if import_batch.scan_batch_id is not None:
                return session.get(ScanBatch, import_batch.scan_batch_id)
            device_ids = session.scalars(
                select(ImportRowResult.device_id).where(
                    ImportRowResult.batch_id == import_batch_id,
                    ImportRowResult.test_status == ImportTestStatus.SUCCESS,
                    ImportRowResult.device_id.is_not(None),
                )
            ).all()
            batch = self._create_batch_in_session(
                session,
                ScanBatchType.IMPORT,
                [device_id for device_id in device_ids if device_id is not None],
                source_import_batch_id=import_batch_id,
            )
            import_batch.scan_batch_id = batch.id
            session.commit()
            session.refresh(batch)
        self._wake_event.set()
        return batch

    def _refresh_batch(self, session: Session, batch_id: int) -> None:
        batch = session.get(ScanBatch, batch_id)
        if batch is None:
            return
        session.flush()
        counts = dict(
            session.execute(
                select(ScanBatchItem.status, func.count())
                .where(ScanBatchItem.batch_id == batch_id)
                .group_by(ScanBatchItem.status)
            ).all()
        )
        batch.total_tasks = sum(counts.values())
        batch.pending_tasks = counts.get(ScanTaskStatus.PENDING, 0)
        batch.running_tasks = counts.get(ScanTaskStatus.RUNNING, 0)
        batch.success_tasks = counts.get(ScanTaskStatus.SUCCESS, 0)
        batch.failed_tasks = counts.get(ScanTaskStatus.FAILED, 0) + counts.get(
            ScanTaskStatus.CANCELLED, 0
        )
        if batch.pending_tasks or batch.running_tasks:
            batch.status = (
                ScanBatchStatus.RUNNING
                if batch.running_tasks or batch.success_tasks or batch.failed_tasks
                else ScanBatchStatus.PENDING
            )
            batch.finished_at = None
        else:
            batch.status = ScanBatchStatus.COMPLETED
            batch.finished_at = datetime.now(timezone.utc)

    def recover_running_tasks(self) -> int:
        with self._enqueue_lock, self.session_factory() as session:
            tasks = session.scalars(
                select(ScanTask).where(ScanTask.status == ScanTaskStatus.RUNNING)
            ).all()
            batch_ids: set[int] = set()
            for task in tasks:
                task.status = ScanTaskStatus.PENDING
                task.started_at = None
                for item in task.items:
                    item.status = ScanTaskStatus.PENDING
                    batch_ids.add(item.batch_id)
            session.flush()
            for batch_id in batch_ids:
                self._refresh_batch(session, batch_id)
            session.commit()
            return len(tasks)

    def _claim_tasks(self, limit: int) -> list[int]:
        if limit <= 0:
            return []
        with self._claim_lock, self.session_factory() as session:
            tasks = session.scalars(
                select(ScanTask)
                .where(ScanTask.status == ScanTaskStatus.PENDING)
                .order_by(ScanTask.priority.desc(), ScanTask.created_at, ScanTask.id)
                .limit(limit)
            ).all()
            if not tasks:
                return []
            started_at = datetime.now(timezone.utc)
            batch_ids: set[int] = set()
            for task in tasks:
                task.status = ScanTaskStatus.RUNNING
                task.started_at = started_at
                for item in task.items:
                    item.status = ScanTaskStatus.RUNNING
                    batch_ids.add(item.batch_id)
            session.flush()
            for batch_id in batch_ids:
                self._refresh_batch(session, batch_id)
            session.commit()
            return [task.id for task in tasks]

    def _claim_next_task(self) -> int | None:
        task_ids = self._claim_tasks(1)
        return task_ids[0] if task_ids else None

    def _execute_task(self, task_id: int) -> None:
        successful = False
        with self.session_factory() as session:
            task = session.get(ScanTask, task_id)
            if task is None or task.status != ScanTaskStatus.RUNNING:
                return
            run = ScanService(
                session,
                self.cipher,
                self.linux_collector,
                self.windows_collector,
            ).run(task.device_id, task.trigger_type)
            task = session.get(ScanTask, task_id)
            if task is None:
                return
            task.scan_run_id = run.id
            task.finished_at = datetime.now(timezone.utc)
            task.error_message = run.error_message
            task.status = (
                ScanTaskStatus.SUCCESS
                if run.status == ScanStatus.SUCCESS
                else ScanTaskStatus.FAILED
            )
            successful = task.status == ScanTaskStatus.SUCCESS
            batch_ids: set[int] = set()
            for item in task.items:
                item.status = task.status
                batch_ids.add(item.batch_id)
            session.flush()
            for batch_id in batch_ids:
                self._refresh_batch(session, batch_id)
            session.commit()
        if successful and self.on_successful_scan:
            try:
                self.on_successful_scan()
            except Exception:
                logger.exception("历史拓扑缓存失效失败")

    def _execute_safely(self, task_id: int) -> None:
        try:
            self._execute_task(task_id)
        except Exception:
            logger.exception("扫描任务 %s 执行异常", task_id)
            self._fail_unexpected_task(task_id)

    def _future_done(self, future: Future) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        self._wake_event.set()

    def _dispatch_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._futures_lock:
                available = self.max_workers - len(self._futures)
            task_ids = self._claim_tasks(available)
            executor = self._executor
            if executor is not None:
                for task_id in task_ids:
                    future = executor.submit(self._execute_safely, task_id)
                    with self._futures_lock:
                        self._futures.add(future)
                    future.add_done_callback(self._future_done)
            if not task_ids:
                self._wake_event.clear()
                self._wake_event.wait(0.5)

    def _fail_unexpected_task(self, task_id: int) -> None:
        with self.session_factory() as session:
            task = session.get(ScanTask, task_id)
            if task is None or task.status not in ACTIVE_TASK_STATUSES:
                return
            task.status = ScanTaskStatus.FAILED
            task.error_message = "扫描任务发生内部错误"
            task.finished_at = datetime.now(timezone.utc)
            batch_ids: set[int] = set()
            for item in task.items:
                item.status = ScanTaskStatus.FAILED
                batch_ids.add(item.batch_id)
            session.flush()
            for batch_id in batch_ids:
                self._refresh_batch(session, batch_id)
            session.commit()

    def start(self) -> None:
        if self._executor is not None:
            return
        self._stop_event.clear()
        self.recover_running_tasks()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="device-scan",
        )
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop,
            name="scan-dispatcher",
            daemon=True,
        )
        self._dispatcher_thread.start()
        self._wake_event.set()

    def shutdown(self) -> None:
        executor = self._executor
        if executor is None:
            return
        self._stop_event.set()
        self._wake_event.set()
        dispatcher = self._dispatcher_thread
        if dispatcher is not None:
            dispatcher.join()
        executor.shutdown(wait=True, cancel_futures=False)
        self._dispatcher_thread = None
        self._executor = None

    def cancel_device(self, device_id: int) -> bool:
        with self._enqueue_lock, self.session_factory() as session:
            task = self._active_task(session, device_id)
            if task is None:
                return True
            if task.status == ScanTaskStatus.RUNNING:
                return False
            task.status = ScanTaskStatus.CANCELLED
            task.finished_at = datetime.now(timezone.utc)
            batch_ids: set[int] = set()
            for item in task.items:
                item.status = ScanTaskStatus.CANCELLED
                batch_ids.add(item.batch_id)
            session.flush()
            for batch_id in batch_ids:
                self._refresh_batch(session, batch_id)
            session.commit()
            return True
