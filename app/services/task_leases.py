from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import (
    ScanBatch,
    ScanBatchItem,
    ScanBatchStatus,
    ScanTask,
    ScanTaskStatus,
)

SCAN_CLAIM_LOCK_KEY = 740_001


class TaskLeaseLost(RuntimeError):
    """Raised when a worker no longer owns a live scan-task lease."""

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        super().__init__(f"scan task lease lost: {task_id}")


def refresh_scan_batches(session: Session, batch_ids: Iterable[int]) -> None:
    """Refresh persisted counters for batches changed by scan task transitions."""
    for batch_id in sorted(set(batch_ids)):
        batch = session.scalar(
            select(ScanBatch).where(ScanBatch.id == batch_id).with_for_update()
        )
        if batch is None:
            continue
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


def _reset_expired_scan_task(task: ScanTask) -> set[int]:
    task.status = ScanTaskStatus.PENDING
    task.worker_id = None
    task.lease_expires_at = None
    task.heartbeat_at = None
    batch_ids: set[int] = set()
    for item in task.items:
        item.status = ScanTaskStatus.PENDING
        batch_ids.add(item.batch_id)
    return batch_ids


def _mark_scan_task_running(task: ScanTask) -> set[int]:
    batch_ids: set[int] = set()
    for item in task.items:
        item.status = ScanTaskStatus.RUNNING
        batch_ids.add(item.batch_id)
    return batch_ids


def claim_scan_tasks(
    session: Session,
    worker_id: str,
    local_capacity: int,
    global_limit: int,
    lease_seconds: int,
) -> list[int]:
    """Claim scan tasks without exceeding the database-wide running limit."""
    if local_capacity <= 0:
        return []

    session.execute(select(func.pg_advisory_xact_lock(SCAN_CLAIM_LOCK_KEY)))
    now = session.scalar(select(func.now()))
    assert now is not None

    expired_tasks = session.scalars(
        select(ScanTask)
        .where(
            ScanTask.status == ScanTaskStatus.RUNNING,
            or_(
                ScanTask.lease_expires_at.is_(None),
                ScanTask.lease_expires_at <= now,
            ),
        )
        .with_for_update(skip_locked=True)
    ).all()
    changed_batch_ids: set[int] = set()
    for task in expired_tasks:
        changed_batch_ids.update(_reset_expired_scan_task(task))
    session.flush()

    active_count = session.scalar(
        select(func.count()).select_from(ScanTask).where(
            ScanTask.status == ScanTaskStatus.RUNNING,
            ScanTask.lease_expires_at > now,
        )
    ) or 0
    claim_limit = min(local_capacity, max(global_limit - active_count, 0))
    if claim_limit:
        tasks = session.scalars(
            select(ScanTask)
            .where(ScanTask.status == ScanTaskStatus.PENDING)
            .order_by(ScanTask.priority.desc(), ScanTask.created_at, ScanTask.id)
            .with_for_update(skip_locked=True)
            .limit(claim_limit)
        ).all()
    else:
        tasks = []

    lease_until = now + timedelta(seconds=lease_seconds)
    for task in tasks:
        task.status = ScanTaskStatus.RUNNING
        task.worker_id = worker_id
        task.started_at = task.started_at or now
        task.heartbeat_at = now
        task.lease_expires_at = lease_until
        task.attempt_count += 1
        changed_batch_ids.update(_mark_scan_task_running(task))
    session.flush()
    refresh_scan_batches(session, changed_batch_ids)
    return [task.id for task in tasks]


def renew_scan_leases(
    session: Session,
    worker_id: str,
    task_ids: Iterable[int],
    lease_seconds: float,
) -> set[int]:
    """Renew live leases owned by ``worker_id`` and return IDs no longer owned."""
    requested_ids = set(task_ids)
    if not requested_ids:
        return set()

    now = session.scalar(select(func.now()))
    assert now is not None
    renewed_ids = set(
        session.scalars(
            update(ScanTask)
            .where(
                ScanTask.id.in_(requested_ids),
                ScanTask.status == ScanTaskStatus.RUNNING,
                ScanTask.worker_id == worker_id,
                ScanTask.lease_expires_at > now,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .returning(ScanTask.id)
        ).all()
    )
    return requested_ids - renewed_ids
