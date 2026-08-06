from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import ImportBatch, ImportBatchStatus, ImportRowResult, ImportTestStatus

IMPORT_TEST_CLAIM_LOCK_KEY = 740_002


class ImportTestLeaseLost(RuntimeError):
    def __init__(self, row_id: int) -> None:
        self.row_id = row_id
        super().__init__(f"import test lease lost: {row_id}")


def refresh_import_batches(session: Session, batch_ids: Iterable[int]) -> set[int]:
    completed: set[int] = set()
    for batch_id in sorted(set(batch_ids)):
        batch = session.scalar(
            select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update()
        )
        if batch is None:
            continue
        was_completed = batch.status == ImportBatchStatus.COMPLETED
        session.flush()
        counts = dict(
            session.execute(
                select(ImportRowResult.test_status, func.count())
                .where(ImportRowResult.batch_id == batch_id)
                .group_by(ImportRowResult.test_status)
            ).all()
        )
        batch.test_pending_rows = counts.get(ImportTestStatus.PENDING, 0)
        batch.test_running_rows = counts.get(ImportTestStatus.RUNNING, 0)
        batch.test_success_rows = counts.get(ImportTestStatus.SUCCESS, 0)
        batch.test_failed_rows = counts.get(ImportTestStatus.FAILED, 0)
        if batch.test_pending_rows or batch.test_running_rows:
            batch.status = ImportBatchStatus.TESTING
            batch.finished_at = None
        else:
            batch.status = ImportBatchStatus.COMPLETED
            batch.finished_at = datetime.now(timezone.utc)
            if not was_completed:
                completed.add(batch_id)
    return completed


def claim_import_tests(
    session: Session,
    worker_id: str,
    local_capacity: int,
    global_limit: int,
    lease_seconds: int,
) -> list[int]:
    if local_capacity <= 0:
        return []
    session.execute(select(func.pg_advisory_xact_lock(IMPORT_TEST_CLAIM_LOCK_KEY)))
    now = session.scalar(select(func.now()))
    assert now is not None
    expired = session.scalars(
        select(ImportRowResult)
        .where(
            ImportRowResult.test_status == ImportTestStatus.RUNNING,
            or_(
                ImportRowResult.test_lease_expires_at.is_(None),
                ImportRowResult.test_lease_expires_at <= now,
            ),
        )
        .with_for_update(skip_locked=True)
    ).all()
    affected = {row.batch_id for row in expired}
    for row in expired:
        row.test_status = ImportTestStatus.PENDING
        row.test_message = None
        row.test_worker_id = None
        row.test_lease_expires_at = None
        row.test_heartbeat_at = None
    session.flush()
    active = session.scalar(
        select(func.count()).select_from(ImportRowResult).where(
            ImportRowResult.test_status == ImportTestStatus.RUNNING,
            ImportRowResult.test_lease_expires_at > now,
        )
    ) or 0
    limit = min(local_capacity, max(global_limit - active, 0))
    rows = (
        session.scalars(
            select(ImportRowResult)
            .where(ImportRowResult.test_status == ImportTestStatus.PENDING)
            .order_by(ImportRowResult.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        ).all()
        if limit
        else []
    )
    expires_at = now + timedelta(seconds=lease_seconds)
    for row in rows:
        row.test_status = ImportTestStatus.RUNNING
        row.test_message = "正在测试连接"
        row.test_worker_id = worker_id
        row.test_lease_expires_at = expires_at
        row.test_heartbeat_at = now
        row.test_attempt_count += 1
        affected.add(row.batch_id)
    session.flush()
    refresh_import_batches(session, affected)
    return [row.id for row in rows]


def renew_import_test_leases(
    session: Session,
    worker_id: str,
    row_ids: Iterable[int],
    lease_seconds: float,
) -> set[int]:
    requested = set(row_ids)
    if not requested:
        return set()
    now = session.scalar(select(func.now()))
    assert now is not None
    renewed = set(
        session.scalars(
            update(ImportRowResult)
            .where(
                ImportRowResult.id.in_(requested),
                ImportRowResult.test_status == ImportTestStatus.RUNNING,
                ImportRowResult.test_worker_id == worker_id,
                ImportRowResult.test_lease_expires_at > now,
            )
            .values(
                test_heartbeat_at=now,
                test_lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .returning(ImportRowResult.id)
        ).all()
    )
    return requested - renewed
