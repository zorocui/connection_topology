from __future__ import annotations

import logging
import threading
import uuid
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
from app.services.database_transactions import (
    TRANSACTION_CONFLICT_MESSAGE,
    DatabaseUnavailable,
    PostgresTransactionRunner,
    TransactionConflict,
)
from app.services.scans import ScanOutcome, ScanService, add_scan_outcome
from app.services.task_leases import (
    TaskLeaseLost,
    claim_scan_tasks,
    refresh_scan_batches,
    renew_scan_leases,
)

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
        transaction_runner: PostgresTransactionRunner,
        *,
        max_workers: int,
        queue_size: int,
        lease_seconds: float = 90,
        heartbeat_seconds: float = 15,
        on_successful_scan: Callable[[], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.linux_collector = linux_collector
        self.windows_collector = windows_collector
        self.transaction_runner = transaction_runner
        self.scan_service = ScanService(
            session_factory,
            cipher,
            linux_collector,
            windows_collector,
        )
        self.max_workers = max_workers
        self.queue_size = queue_size
        self.lease_seconds = lease_seconds
        self.task_heartbeat_seconds = heartbeat_seconds
        self.worker_id = uuid.uuid4().hex
        self.on_successful_scan = on_successful_scan
        self._enqueue_lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._heartbeat_stop_event = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        self._dispatcher_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._futures: set[Future] = set()
        self._futures_lock = threading.Lock()
        self._active_task_ids: set[int] = set()
        self._lost_task_ids: set[int] = set()
        self._active_tasks_lock = threading.Lock()

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
        def enqueue(session: Session) -> int:
            task = self._enqueue_in_session(
                session, device_id, trigger, priority, batch_id
            )
            session.flush()
            if batch_id is not None:
                self._refresh_batch(session, batch_id)
            return task.id

        with self._enqueue_lock:
            task_id = self.transaction_runner.run("enqueue_scan_device", enqueue)
        with self.session_factory() as session:
            task = session.get(ScanTask, task_id)
            assert task is not None
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
        def create(session: Session) -> int:
            batch = self._create_batch_in_session(
                session,
                batch_type,
                device_ids,
                cluster_id=cluster_id,
                source_import_batch_id=source_import_batch_id,
            )
            return batch.id

        with self._enqueue_lock:
            batch_id = self.transaction_runner.run("create_scan_batch", create)
        with self.session_factory() as session:
            batch = session.get(ScanBatch, batch_id)
            assert batch is not None
        self._wake_event.set()
        return batch

    def create_import_scan_batch(self, import_batch_id: int) -> ScanBatch | None:
        def create(session: Session) -> int | None:
            import_batch = session.get(ImportBatch, import_batch_id)
            if import_batch is None:
                return None
            if import_batch.scan_batch_id is not None:
                return import_batch.scan_batch_id
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
            return batch.id

        with self._enqueue_lock:
            batch_id = self.transaction_runner.run("create_import_scan_batch", create)
        if batch_id is None:
            return None
        with self.session_factory() as session:
            batch = session.get(ScanBatch, batch_id)
            assert batch is not None
        self._wake_event.set()
        return batch

    def _refresh_batch(self, session: Session, batch_id: int) -> None:
        refresh_scan_batches(session, {batch_id})

    def _claim_tasks(self, limit: int) -> list[int]:
        task_ids = self.transaction_runner.run(
            "claim_scan_tasks",
            lambda session: claim_scan_tasks(
                session,
                self.worker_id,
                limit,
                self.max_workers,
                self.lease_seconds,
            ),
        )
        if task_ids:
            with self._active_tasks_lock:
                self._active_task_ids.update(task_ids)
                self._lost_task_ids.difference_update(task_ids)
        return task_ids

    def _claim_next_task(self) -> int | None:
        task_ids = self._claim_tasks(1)
        return task_ids[0] if task_ids else None

    def _execute_task(self, task_id: int) -> None:
        try:
            with self.session_factory() as session:
                task = session.scalar(
                    select(ScanTask).where(
                        ScanTask.id == task_id,
                        ScanTask.status == ScanTaskStatus.RUNNING,
                        ScanTask.worker_id == self.worker_id,
                        ScanTask.lease_expires_at > func.now(),
                    )
                )
                if task is None:
                    raise TaskLeaseLost(task_id)
                device_id = task.device_id
                trigger = task.trigger_type

            outcome = self.scan_service.collect(device_id, trigger)
            self._raise_if_lease_lost(task_id)

            try:
                successful = self.transaction_runner.run(
                    "persist_scan",
                    lambda session: self._persist_outcome(session, task_id, outcome),
                )
            except TransactionConflict:
                try:
                    self._record_transaction_conflict(task_id, outcome)
                except (DatabaseUnavailable, TransactionConflict):
                    logger.error("扫描任务 %s 事务冲突状态无法保存", task_id)
                return
            if successful and self.on_successful_scan:
                try:
                    self.on_successful_scan()
                except Exception:
                    logger.exception("历史拓扑缓存失效失败")
        finally:
            self._release_active_task(task_id)

    def _persist_outcome(
        self,
        session: Session,
        task_id: int,
        outcome: ScanOutcome,
    ) -> bool:
        task = session.scalar(
            select(ScanTask)
            .where(
                ScanTask.id == task_id,
                ScanTask.status == ScanTaskStatus.RUNNING,
                ScanTask.worker_id == self.worker_id,
                ScanTask.lease_expires_at > func.now(),
            )
            .with_for_update()
        )
        if task is None:
            raise TaskLeaseLost(task_id)
        run = add_scan_outcome(session, outcome)
        task.scan_run_id = run.id
        task.finished_at = outcome.finished_at
        task.error_message = outcome.error_message
        task.status = (
            ScanTaskStatus.SUCCESS
            if outcome.status == ScanStatus.SUCCESS
            else ScanTaskStatus.FAILED
        )
        task.worker_id = None
        task.lease_expires_at = None
        task.heartbeat_at = None
        batch_ids: set[int] = set()
        for item in task.items:
            item.status = task.status
            batch_ids.add(item.batch_id)
        session.flush()
        for batch_id in batch_ids:
            self._refresh_batch(session, batch_id)
        return task.status == ScanTaskStatus.SUCCESS

    def _record_transaction_conflict(self, task_id: int, outcome: ScanOutcome) -> None:
        conflict_outcome = ScanOutcome(
            device_id=outcome.device_id,
            trigger=outcome.trigger,
            status=ScanStatus.FAILED,
            started_at=outcome.started_at,
            finished_at=datetime.now(timezone.utc),
            error_code="transaction_conflict",
            error_message=TRANSACTION_CONFLICT_MESSAGE,
        )
        self.transaction_runner.run(
            "record_transaction_conflict",
            lambda session: self._persist_outcome(session, task_id, conflict_outcome),
        )

    def _execute_safely(self, task_id: int) -> None:
        try:
            self._execute_task(task_id)
        except TaskLeaseLost:
            self._log_lease_lost(task_id)
        except (DatabaseUnavailable, TransactionConflict) as exc:
            logger.error("扫描任务 %s 数据库操作失败: %s", task_id, str(exc))
            try:
                self._fail_unexpected_task(task_id)
            except TaskLeaseLost:
                self._log_lease_lost(task_id)
            except (DatabaseUnavailable, TransactionConflict):
                logger.error("扫描任务 %s 失败状态无法保存", task_id)
        except Exception as exc:  # noqa: BLE001 - convert unexpected worker failure to state
            logger.error(
                "扫描任务 %s 执行异常 error_type=%s",
                task_id,
                type(exc).__name__,
            )
            try:
                self._fail_unexpected_task(task_id)
            except TaskLeaseLost:
                self._log_lease_lost(task_id)
            except (DatabaseUnavailable, TransactionConflict):
                logger.error("扫描任务 %s 失败状态无法保存", task_id)

    def _log_lease_lost(self, task_id: int) -> None:
        logger.info(
            "scan lease lost operation=execute_scan task_id=%s worker_id=%s",
            task_id,
            self.worker_id,
        )

    def _raise_if_lease_lost(self, task_id: int) -> None:
        with self._active_tasks_lock:
            if task_id in self._lost_task_ids:
                raise TaskLeaseLost(task_id)

    def _release_active_task(self, task_id: int) -> None:
        with self._active_tasks_lock:
            self._active_task_ids.discard(task_id)
            self._lost_task_ids.discard(task_id)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop_event.wait(self.task_heartbeat_seconds):
            with self._active_tasks_lock:
                task_ids = set(self._active_task_ids)
            if not task_ids:
                continue
            try:
                lost_ids = self.transaction_runner.run(
                    "renew_scan_leases",
                    lambda session, active_ids=task_ids: renew_scan_leases(
                        session,
                        self.worker_id,
                        active_ids,
                        self.lease_seconds,
                    ),
                )
            except (DatabaseUnavailable, TransactionConflict):
                logger.error(
                    "scan lease heartbeat failed worker_id=%s",
                    self.worker_id,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - keep the shared heartbeat alive
                logger.error(
                    "scan lease heartbeat failed worker_id=%s error_type=%s",
                    self.worker_id,
                    type(exc).__name__,
                )
                continue
            if lost_ids:
                with self._active_tasks_lock:
                    self._active_task_ids.difference_update(lost_ids)
                    self._lost_task_ids.update(lost_ids)

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
        def fail(session: Session) -> None:
            task = session.scalar(
                select(ScanTask)
                .where(
                    ScanTask.id == task_id,
                    ScanTask.status == ScanTaskStatus.RUNNING,
                    ScanTask.worker_id == self.worker_id,
                    ScanTask.lease_expires_at > func.now(),
                )
                .with_for_update()
            )
            if task is None:
                raise TaskLeaseLost(task_id)
            task.status = ScanTaskStatus.FAILED
            task.error_message = "扫描任务发生内部错误"
            task.finished_at = datetime.now(timezone.utc)
            task.worker_id = None
            task.lease_expires_at = None
            task.heartbeat_at = None
            batch_ids: set[int] = set()
            for item in task.items:
                item.status = ScanTaskStatus.FAILED
                batch_ids.add(item.batch_id)
            session.flush()
            for batch_id in batch_ids:
                self._refresh_batch(session, batch_id)

        self.transaction_runner.run("fail_scan_task", fail)

    def start(self) -> None:
        if self._executor is not None:
            return
        self._stop_event.clear()
        self._heartbeat_stop_event.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="device-scan",
        )
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop,
            name="scan-dispatcher",
            daemon=True,
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="scan-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
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
        self._heartbeat_stop_event.set()
        heartbeat = self._heartbeat_thread
        if heartbeat is not None:
            heartbeat.join()
        self._dispatcher_thread = None
        self._heartbeat_thread = None
        self._executor = None

    def cancel_device(self, device_id: int, *, session: Session | None = None) -> bool:
        def cancel(session: Session) -> bool:
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
            return True

        with self._enqueue_lock:
            if session is not None:
                return cancel(session)
            return self.transaction_runner.run("cancel_scan_device", cancel)
