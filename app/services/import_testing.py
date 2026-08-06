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
from app.services.database_transactions import (
    DatabaseUnavailable,
    PostgresTransactionRunner,
    TransactionConflict,
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
        executor: ThreadPoolExecutor,
        linux_collector: Collector,
        windows_collector: Collector,
        transaction_runner: PostgresTransactionRunner | Callable[[int], None] | None = None,
        batch_completed_callback: Callable[[int], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.executor = executor
        self.linux_collector = linux_collector
        self.windows_collector = windows_collector
        if transaction_runner is not None and not isinstance(
            transaction_runner, PostgresTransactionRunner
        ):
            batch_completed_callback = transaction_runner
            transaction_runner = None
        self.transaction_runner = transaction_runner or PostgresTransactionRunner(
            session_factory,
            (0.1, 0.3),
        )
        self.batch_completed_callback = batch_completed_callback

    def schedule_batch(self, batch_id: int) -> None:
        with self.session_factory() as session:
            row_ids = list(
                session.scalars(
                    select(ImportRowResult.id).where(
                        ImportRowResult.batch_id == batch_id,
                        ImportRowResult.test_status == ImportTestStatus.PENDING,
                    )
                ).all()
            )
        for row_id in row_ids:
            self._submit(row_id)

    def resume_pending(self) -> None:
        def recover(session: Session) -> list[int]:
            running_rows = list(
                session.scalars(
                    select(ImportRowResult).where(
                        ImportRowResult.test_status == ImportTestStatus.RUNNING
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
            return list(
                session.scalars(
                    select(ImportRowResult.id).where(
                        ImportRowResult.test_status == ImportTestStatus.PENDING
                    )
                ).all()
            )

        row_ids = self.transaction_runner.run("recover_import_tests", recover)
        for row_id in row_ids:
            self._submit(row_id)
        if self.batch_completed_callback:
            with self.session_factory() as session:
                completed_batch_ids = list(
                    session.scalars(
                        select(ImportBatch.id).where(
                            ImportBatch.status == ImportBatchStatus.COMPLETED,
                            ImportBatch.scan_batch_id.is_(None),
                        )
                    ).all()
                )
            for batch_id in completed_batch_ids:
                self.batch_completed_callback(batch_id)

    def _refresh_batch_counts(self, session: Session, batch_id: int) -> bool:
        batch = session.scalar(
            select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update()
        )
        assert batch is not None
        was_completed = batch.status == ImportBatchStatus.COMPLETED

        def count(status: ImportTestStatus) -> int:
            return session.scalar(
                select(func.count()).select_from(ImportRowResult).where(
                    ImportRowResult.batch_id == batch_id,
                    ImportRowResult.test_status == status,
                )
            ) or 0

        batch.test_pending_rows = count(ImportTestStatus.PENDING)
        batch.test_running_rows = count(ImportTestStatus.RUNNING)
        batch.test_success_rows = count(ImportTestStatus.SUCCESS)
        batch.test_failed_rows = count(ImportTestStatus.FAILED)
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
        def load(session: Session) -> tuple[int, ImportTestTarget | None] | None:
            row = session.get(ImportRowResult, row_id)
            if row is None or row.test_status != ImportTestStatus.PENDING:
                return None
            batch_id = row.batch_id
            device = session.get(Device, row.device_id)
            target = (
                None
                if device is None
                else ImportTestTarget(
                    device_id=device.id,
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
            return batch_id, target

        return self.transaction_runner.run("claim_import_test_row", load)

    def _test_target(self, target: ImportTestTarget | None) -> ImportTestOutcome:
        if target is None:
            return ImportTestOutcome(ImportTestStatus.FAILED, "导入设备不存在")
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
                    device_id=target.device_id,
                ),
                password,
            )
            return ImportTestOutcome(ImportTestStatus.SUCCESS, "连接测试成功")
        except Exception as exc:  # noqa: BLE001 - persist per-device test failure
            return ImportTestOutcome(
                ImportTestStatus.FAILED,
                safe_error_message(str(exc), (password,)),
            )

    def _save_result(self, row_id: int, outcome: ImportTestOutcome) -> int | None:
        def save(session: Session) -> int | None:
            row = session.get(ImportRowResult, row_id)
            if row is None or row.test_status not in {
                ImportTestStatus.PENDING,
                ImportTestStatus.RUNNING,
            }:
                return None
            row.test_status = outcome.status
            row.test_message = outcome.message
            session.flush()
            return row.batch_id if self._refresh_batch_counts(session, row.batch_id) else None

        return self.transaction_runner.run("save_import_test_result", save)

    def test_row(self, row_id: int) -> None:
        try:
            loaded = self._load_target(row_id)
            if loaded is None:
                return
            _, target = loaded
            outcome = self._test_target(target)
        except (DatabaseUnavailable, TransactionConflict) as exc:
            logger.error(
                "导入连接测试数据库操作失败，行记录 %s: %s",
                row_id,
                str(exc),
            )
            return
        except Exception as exc:  # noqa: BLE001 - persist a sanitized per-row failure
            logger.error(
                "导入连接测试任务发生内部异常，行记录 %s error_type=%s",
                row_id,
                type(exc).__name__,
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
            if isinstance(exception, (DatabaseUnavailable, TransactionConflict)):
                logger.error("导入连接测试后台数据库操作失败: %s", str(exception))
            else:
                logger.error(
                    "导入连接测试后台任务异常 error_type=%s",
                    type(exception).__name__,
                )

    def _submit(self, row_id: int) -> None:
        future = self.executor.submit(self.test_row, row_id)
        if future is not None and hasattr(future, "add_done_callback"):
            future.add_done_callback(self._future_done)
