import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
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

    def schedule_batch(self, batch_id: int) -> None:
        with self.session_factory() as session:
            row_ids = session.scalars(
                select(ImportRowResult.id).where(
                    ImportRowResult.batch_id == batch_id,
                    ImportRowResult.test_status == ImportTestStatus.PENDING,
                )
            ).all()
        for row_id in row_ids:
            self._submit(row_id)

    def resume_pending(self) -> None:
        with self.session_factory() as session:
            row_ids = session.scalars(
                select(ImportRowResult.id).where(
                    ImportRowResult.test_status == ImportTestStatus.PENDING
                )
            ).all()
        for row_id in row_ids:
            self._submit(row_id)
        if self.batch_completed_callback:
            with self.session_factory() as session:
                completed_batch_ids = session.scalars(
                    select(ImportBatch.id).where(
                        ImportBatch.status == ImportBatchStatus.COMPLETED,
                        ImportBatch.scan_batch_id.is_(None),
                    )
                ).all()
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
        if batch.test_pending_rows == 0:
            batch.status = ImportBatchStatus.COMPLETED
            batch.finished_at = datetime.now(timezone.utc)
        return not was_completed and batch.status == ImportBatchStatus.COMPLETED

    def _load_target(
        self,
        row_id: int,
    ) -> tuple[int, ImportTestTarget | None] | None:
        with self.session_factory() as session:
            row = session.get(ImportRowResult, row_id)
            if row is None or row.test_status != ImportTestStatus.PENDING:
                return None
            batch_id = row.batch_id
            device = session.get(Device, row.device_id)
            if device is None:
                return batch_id, None
            return batch_id, ImportTestTarget(
                os_type=device.os_type,
                host=device.host,
                port=device.port,
                username=device.username,
                encrypted_password=device.encrypted_password,
            )

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
        completed_batch_id: int | None = None
        with self.session_factory() as session:
            row = session.get(ImportRowResult, row_id)
            if row is None or row.test_status != ImportTestStatus.PENDING:
                return None
            row.test_status = outcome.status
            row.test_message = outcome.message
            session.flush()
            if self._refresh_batch_counts(session, row.batch_id):
                completed_batch_id = row.batch_id
            session.commit()
        return completed_batch_id

    def test_row(self, row_id: int) -> None:
        loaded = self._load_target(row_id)
        if loaded is None:
            return
        _, target = loaded
        outcome = self._test_target(target)
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
