from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import Collector, DeviceConnectionSpec
from app.models import Device, ImportRowResult, ImportTestStatus, OSType
from app.security import CredentialCipher, safe_error_message
from app.services.database_transactions import (
    DATABASE_UNAVAILABLE_MESSAGE,
    DatabaseUnavailable,
    PostgresTransactionRunner,
    TransactionConflict,
)
from app.services.import_test_leases import (
    IMPORT_TEST_CLAIM_LOCK_KEY,
    ImportTestLeaseLost,
    claim_import_tests,
    refresh_import_batches,
    renew_import_test_leases,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportTestTarget:
    device_id: int
    os_type: OSType
    host: str
    port: int
    username: str
    encrypted_password: str


@dataclass(frozen=True)
class ImportTestOutcome:
    status: ImportTestStatus
    message: str


class ImportTestService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cipher: CredentialCipher,
        executor: ThreadPoolExecutor | object | None,
        linux_collector: Collector,
        windows_collector: Collector,
        transaction_runner: PostgresTransactionRunner | Callable[[int], None] | None = None,
        batch_completed_callback: Callable[[int], None] | None = None,
        *,
        max_workers: int = 20,
        global_limit: int = 20,
        lease_seconds: int = 90,
        heartbeat_seconds: float = 15,
        worker_id: str | None = None,
    ) -> None:
        if transaction_runner is not None and not isinstance(
            transaction_runner, PostgresTransactionRunner
        ):
            batch_completed_callback = transaction_runner
            transaction_runner = None
        self.session_factory = session_factory
        self.cipher = cipher
        self.linux_collector = linux_collector
        self.windows_collector = windows_collector
        self.transaction_runner = transaction_runner or PostgresTransactionRunner(
            session_factory, (0.1, 0.3)
        )
        self.batch_completed_callback = batch_completed_callback
        self.worker_id = worker_id or str(uuid.uuid4())
        inferred = getattr(executor, "_max_workers", None)
        self.max_workers = int(inferred or max_workers)
        self.global_limit = global_limit
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._executor = executor
        self._owns_executor = executor is None
        self._dispatcher_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._heartbeat_stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._futures: set[Future] = set()
        self._futures_lock = threading.Lock()
        self._active_ids: set[int] = set()
        self._lost_ids: set[int] = set()
        self._active_lock = threading.Lock()

    def start(self) -> None:
        if self._dispatcher_thread is not None:
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="import-test"
            )
        self._stop_event.clear()
        self._heartbeat_stop_event.clear()
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop, name="import-test-dispatcher", daemon=True
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="import-test-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        self._dispatcher_thread.start()
        self._wake_event.set()

    def shutdown(self) -> None:
        dispatcher = self._dispatcher_thread
        if dispatcher is None:
            return
        self._stop_event.set()
        self._wake_event.set()
        dispatcher.join()
        if self._owns_executor and isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None
        self._heartbeat_stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join()
        self._dispatcher_thread = None
        self._heartbeat_thread = None

    def schedule_batch(self, batch_id: int) -> None:
        del batch_id
        if self._dispatcher_thread is None and self._executor is not None:
            self._dispatch_legacy_until_empty()
        else:
            self._wake_event.set()

    def resume_pending(self) -> None:
        if self._owns_executor:
            self.start()
            self._wake_event.set()
        else:
            self.schedule_batch(0)

    def _claim(self, capacity: int) -> list[int]:
        if capacity <= 0:
            return []
        return self.transaction_runner.run(
            "claim_import_tests",
            lambda session: claim_import_tests(
                session,
                self.worker_id,
                capacity,
                self.global_limit,
                self.lease_seconds,
            ),
        )

    def _dispatch_legacy_until_empty(self) -> None:
        while True:
            row_ids = self._claim(self.max_workers)
            if not row_ids:
                return
            for row_id in row_ids:
                future = self._executor.submit(self.test_row, row_id)
                if future is not None and hasattr(future, "add_done_callback"):
                    future.add_done_callback(self._future_done)
            if isinstance(self._executor, ThreadPoolExecutor):
                return

    def _dispatch_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._futures_lock:
                capacity = self.max_workers - len(self._futures)
            try:
                row_ids = self._claim(capacity)
            except (DatabaseUnavailable, TransactionConflict):
                logger.error("import test claim failed worker_id=%s", self.worker_id)
                row_ids = []
            for row_id in row_ids:
                future = self._executor.submit(self.test_row, row_id)
                if isinstance(future, Future):
                    with self._futures_lock:
                        self._futures.add(future)
                    future.add_done_callback(self._future_done)
            if not row_ids:
                self._wake_event.clear()
                self._wake_event.wait(0.5)

    def _future_done(self, future: Future) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        if not future.cancelled() and future.exception() is not None:
            exception = future.exception()
            if isinstance(exception, (DatabaseUnavailable, TransactionConflict)):
                logger.error(
                    "导入连接测试后台数据库操作失败: %s",
                    DATABASE_UNAVAILABLE_MESSAGE,
                )
            else:
                logger.error(
                    "import test background failure error_type=%s",
                    type(exception).__name__,
                )
        self._wake_event.set()

    def _claim_specific(self, session: Session, row_id: int):
        from datetime import timedelta

        now = session.scalar(select(func.now()))
        row = session.get(ImportRowResult, row_id)
        if row is not None and row.test_status == ImportTestStatus.PENDING:
            session.execute(select(func.pg_advisory_xact_lock(IMPORT_TEST_CLAIM_LOCK_KEY)))
            active = session.scalar(
                select(func.count()).select_from(ImportRowResult).where(
                    ImportRowResult.test_status == ImportTestStatus.RUNNING,
                    ImportRowResult.test_lease_expires_at > now,
                )
            ) or 0
            if active < self.global_limit:
                row.test_status = ImportTestStatus.RUNNING
                row.test_message = "正在测试连接"
                row.test_worker_id = self.worker_id
                row.test_heartbeat_at = now
                row.test_lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                row.test_attempt_count += 1
                session.flush()
                refresh_import_batches(session, {row.batch_id})
        return now

    def _load_target(self, row_id: int) -> ImportTestTarget | None:
        def load(session: Session) -> ImportTestTarget | None:
            now = self._claim_specific(session, row_id)
            row = session.scalar(
                select(ImportRowResult).where(
                    ImportRowResult.id == row_id,
                    ImportRowResult.test_status == ImportTestStatus.RUNNING,
                    ImportRowResult.test_worker_id == self.worker_id,
                    ImportRowResult.test_lease_expires_at > now,
                )
            )
            if row is None:
                raise ImportTestLeaseLost(row_id)
            device = session.get(Device, row.device_id)
            if device is None:
                return None
            return ImportTestTarget(
                device_id=device.id,
                os_type=device.os_type,
                host=device.host,
                port=device.port,
                username=device.username,
                encrypted_password=device.encrypted_password,
            )

        return self.transaction_runner.run("claim_import_test_row", load)

    def _test_target(self, target: ImportTestTarget | None) -> ImportTestOutcome:
        if target is None:
            return ImportTestOutcome(ImportTestStatus.FAILED, "导入设备不存在")
        password = ""
        try:
            password = self.cipher.decrypt(target.encrypted_password)
            collector = (
                self.linux_collector if target.os_type == OSType.LINUX else self.windows_collector
            )
            collector.test_connection(
                DeviceConnectionSpec(
                    host=target.host,
                    port=target.port,
                    username=target.username,
                    device_id=target.device_id,
                ),
                password,
            )
            return ImportTestOutcome(ImportTestStatus.SUCCESS, "连接测试成功")
        except Exception as exc:  # noqa: BLE001
            return ImportTestOutcome(
                ImportTestStatus.FAILED, safe_error_message(str(exc), (password,))
            )

    def _save_result_for_worker(
        self,
        row_id: int,
        worker_id: str,
        status: ImportTestStatus,
        message: str,
    ) -> int | None:
        def save(session: Session) -> int | None:
            now = session.scalar(select(func.now()))
            row = session.scalar(
                select(ImportRowResult)
                .where(
                    ImportRowResult.id == row_id,
                    ImportRowResult.test_status == ImportTestStatus.RUNNING,
                    ImportRowResult.test_worker_id == worker_id,
                    ImportRowResult.test_lease_expires_at > now,
                )
                .with_for_update()
            )
            if row is None:
                raise ImportTestLeaseLost(row_id)
            row.test_status = status
            row.test_message = message
            row.test_worker_id = None
            row.test_lease_expires_at = None
            row.test_heartbeat_at = None
            session.flush()
            completed = refresh_import_batches(session, {row.batch_id})
            return row.batch_id if row.batch_id in completed else None

        return self.transaction_runner.run("save_import_test_result", save)

    def _save_result(self, row_id: int, outcome: ImportTestOutcome) -> int | None:
        return self._save_result_for_worker(
            row_id, self.worker_id, outcome.status, outcome.message
        )

    def test_row(self, row_id: int) -> None:
        with self._active_lock:
            self._active_ids.add(row_id)
        try:
            target = self._load_target(row_id)
            outcome = self._test_target(target)
            with self._active_lock:
                if row_id in self._lost_ids:
                    raise ImportTestLeaseLost(row_id)
            completed = self._save_result(row_id, outcome)
            if completed is not None and self.batch_completed_callback:
                self.batch_completed_callback(completed)
        except ImportTestLeaseLost:
            logger.info(
                "import test lease lost row_id=%s worker_id=%s", row_id, self.worker_id
            )
        except (DatabaseUnavailable, TransactionConflict):
            logger.error("import test database operation failed row_id=%s", row_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "import test internal failure row_id=%s error_type=%s",
                row_id,
                type(exc).__name__,
            )
            try:
                self.transaction_runner.run(
                    "claim_failed_import_test_row",
                    lambda session: self._claim_specific(session, row_id),
                )
                self._save_result(
                    row_id,
                    ImportTestOutcome(
                        ImportTestStatus.FAILED,
                        f"连接测试内部异常：{safe_error_message(str(exc), ())}",
                    ),
                )
            except ImportTestLeaseLost:
                pass
        finally:
            with self._active_lock:
                self._active_ids.discard(row_id)
                self._lost_ids.discard(row_id)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop_event.wait(self.heartbeat_seconds):
            with self._active_lock:
                row_ids = set(self._active_ids)
            if not row_ids:
                continue
            try:
                lost = self.transaction_runner.run(
                    "renew_import_test_leases",
                    lambda session, active_ids=row_ids: renew_import_test_leases(
                        session, self.worker_id, active_ids, self.lease_seconds
                    ),
                )
            except (DatabaseUnavailable, TransactionConflict):
                logger.error("import test heartbeat failed worker_id=%s", self.worker_id)
                continue
            if lost:
                with self._active_lock:
                    self._active_ids.difference_update(lost)
                    self._lost_ids.update(lost)
