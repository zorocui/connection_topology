import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import Collector, DeviceConnectionSpec
from app.models import (
    Device,
    ImportBatch,
    ImportBatchStatus,
    ImportRowResult,
    ImportTestStatus,
    OSType,
)
from app.security import CredentialCipher, safe_error_message

logger = logging.getLogger(__name__)
T = TypeVar("T")
DATABASE_RETRY_DELAYS = (0.1, 0.3)


def _is_transient_database_error(exc: Exception) -> bool:
    if isinstance(exc, SATimeoutError):
        return True
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database" in message and (
        "locked" in message or "busy" in message
    )


@dataclass(frozen=True)
class ImportTestTarget:
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
        executor: ThreadPoolExecutor,
        linux_collector: Collector,
        windows_collector: Collector,
        batch_completed_callback: Callable[[int], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.executor = executor
        self.linux_collector = linux_collector
        self.windows_collector = windows_collector
        self.batch_completed_callback = batch_completed_callback
        self._database_gate = threading.Lock()

    def _run_database_operation(self, operation: Callable[[], T]) -> T:
        for attempt in range(len(DATABASE_RETRY_DELAYS) + 1):
            try:
                with self._database_gate:
                    return operation()
            except Exception as exc:
                if (
                    not _is_transient_database_error(exc)
                    or attempt == len(DATABASE_RETRY_DELAYS)
                ):
                    raise
                time.sleep(DATABASE_RETRY_DELAYS[attempt])
        raise AssertionError("数据库重试循环未返回")

    def schedule_batch(self, batch_id: int) -> None:
        def load_row_ids() -> list[int]:
            with self.session_factory() as session:
                return list(
                    session.scalars(
                        select(ImportRowResult.id).where(
                            ImportRowResult.batch_id == batch_id,
                            ImportRowResult.test_status == ImportTestStatus.PENDING,
                        )
                    ).all()
                )

        row_ids = self._run_database_operation(load_row_ids)
        for row_id in row_ids:
            self._submit(row_id)

    def resume_pending(self) -> None:
        def recover_and_load_pending_row_ids() -> list[int]:
            with self.session_factory() as session:
                running_rows = list(
                    session.scalars(
                        select(ImportRowResult).where(
                            ImportRowResult.test_status
                            == ImportTestStatus.RUNNING
                        )
                    ).all()
                )
                affected_batch_ids = {row.batch_id for row in running_rows}
                for row in running_rows:
                    row.test_status = ImportTestStatus.PENDING
                    row.test_message = None
                session.flush()
                for batch_id in affected_batch_ids:
                    self._refresh_batch_counts(session, batch_id)
                row_ids = list(
                    session.scalars(
                        select(ImportRowResult.id).where(
                            ImportRowResult.test_status == ImportTestStatus.PENDING
                        )
                    ).all()
                )
                session.commit()
                return row_ids

        row_ids = self._run_database_operation(
            recover_and_load_pending_row_ids
        )
        for row_id in row_ids:
            self._submit(row_id)
        if self.batch_completed_callback:
            def load_completed_batch_ids() -> list[int]:
                with self.session_factory() as session:
                    return list(
                        session.scalars(
                            select(ImportBatch.id).where(
                                ImportBatch.status == ImportBatchStatus.COMPLETED,
                                ImportBatch.scan_batch_id.is_(None),
                            )
                        ).all()
                    )

            completed_batch_ids = self._run_database_operation(
                load_completed_batch_ids
            )
            for batch_id in completed_batch_ids:
                self.batch_completed_callback(batch_id)

    def _refresh_batch_counts(self, session: Session, batch_id: int) -> bool:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        was_completed = batch.status == ImportBatchStatus.COMPLETED
        batch.test_pending_rows = session.scalar(
            select(func.count()).select_from(ImportRowResult).where(
                ImportRowResult.batch_id == batch_id,
                ImportRowResult.test_status == ImportTestStatus.PENDING,
            )
        ) or 0
        batch.test_running_rows = session.scalar(
            select(func.count()).select_from(ImportRowResult).where(
                ImportRowResult.batch_id == batch_id,
                ImportRowResult.test_status == ImportTestStatus.RUNNING,
            )
        ) or 0
        batch.test_success_rows = session.scalar(
            select(func.count()).select_from(ImportRowResult).where(
                ImportRowResult.batch_id == batch_id,
                ImportRowResult.test_status == ImportTestStatus.SUCCESS,
            )
        ) or 0
        batch.test_failed_rows = session.scalar(
            select(func.count()).select_from(ImportRowResult).where(
                ImportRowResult.batch_id == batch_id,
                ImportRowResult.test_status == ImportTestStatus.FAILED,
            )
        ) or 0
        if batch.test_pending_rows + batch.test_running_rows == 0:
            batch.status = ImportBatchStatus.COMPLETED
            batch.finished_at = datetime.now(timezone.utc)
        else:
            batch.status = ImportBatchStatus.TESTING
            batch.finished_at = None
        return not was_completed and batch.status == ImportBatchStatus.COMPLETED

    def _load_target(
        self,
        row_id: int,
    ) -> tuple[int, ImportTestTarget | None] | None:
        def load() -> tuple[int, ImportTestTarget | None] | None:
            with self.session_factory() as session:
                row = session.get(ImportRowResult, row_id)
                if row is None or row.test_status != ImportTestStatus.PENDING:
                    return None
                batch_id = row.batch_id
                device = session.get(Device, row.device_id)
                target = (
                    None
                    if device is None
                    else ImportTestTarget(
                        os_type=device.os_type,
                        host=device.host,
                        port=device.port,
                        username=device.username,
                        encrypted_password=device.encrypted_password,
                    )
                )
                row.test_status = ImportTestStatus.RUNNING
                row.test_message = "正在测试连接"
                session.flush()
                self._refresh_batch_counts(session, batch_id)
                session.commit()
                return batch_id, target

        return self._run_database_operation(load)

    def _test_target(
        self,
        target: ImportTestTarget | None,
    ) -> ImportTestOutcome:
        if target is None:
            return ImportTestOutcome(
                ImportTestStatus.FAILED,
                "导入设备不存在",
            )
        password = ""
        try:
            password = self.cipher.decrypt(target.encrypted_password)
            collector = (
                self.linux_collector
                if target.os_type == OSType.LINUX
                else self.windows_collector
            )
            collector.test_connection(
                DeviceConnectionSpec(
                    host=target.host,
                    port=target.port,
                    username=target.username,
                ),
                password,
            )
            return ImportTestOutcome(
                ImportTestStatus.SUCCESS,
                "连接测试成功",
            )
        except Exception as exc:  # noqa: BLE001 - persist per-device test failure
            return ImportTestOutcome(
                ImportTestStatus.FAILED,
                safe_error_message(str(exc), (password,)),
            )

    def _save_result(
        self,
        row_id: int,
        outcome: ImportTestOutcome,
    ) -> int | None:
        def save() -> int | None:
            completed_batch_id: int | None = None
            with self.session_factory() as session:
                row = session.get(ImportRowResult, row_id)
                if row is None or row.test_status not in {
                    ImportTestStatus.PENDING,
                    ImportTestStatus.RUNNING,
                }:
                    return None
                row.test_status = outcome.status
                row.test_message = outcome.message
                session.flush()
                if self._refresh_batch_counts(session, row.batch_id):
                    completed_batch_id = row.batch_id
                session.commit()
            return completed_batch_id

        return self._run_database_operation(save)

    def test_row(self, row_id: int) -> None:
        try:
            loaded = self._load_target(row_id)
            if loaded is None:
                return
            _, target = loaded
            outcome = self._test_target(target)
        except Exception as exc:
            logger.exception(
                "导入连接测试任务发生内部异常，行记录 %s",
                row_id,
            )
            outcome = ImportTestOutcome(
                ImportTestStatus.FAILED,
                f"连接测试内部异常：{safe_error_message(str(exc), ())}",
            )
        completed_batch_id = self._save_result(row_id, outcome)
        if completed_batch_id is not None and self.batch_completed_callback:
            self.batch_completed_callback(completed_batch_id)

    def _future_done(self, future: Future) -> None:
        if future.cancelled():
            return
        exception = future.exception()
        if exception is not None:
            logger.error(
                "导入连接测试后台任务异常",
                exc_info=(
                    type(exception),
                    exception,
                    exception.__traceback__,
                ),
            )

    def _submit(self, row_id: int) -> None:
        future = self.executor.submit(self.test_row, row_id)
        if future is not None and hasattr(future, "add_done_callback"):
            future.add_done_callback(self._future_done)
