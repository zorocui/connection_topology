from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Cluster,
    Device,
    ScanBatch,
    ScanBatchItem,
    ScanBatchStatus,
    ScanTask,
    ScanTaskStatus,
)

FAILED_BATCH_ITEM_STATUSES = (
    ScanTaskStatus.FAILED,
    ScanTaskStatus.CANCELLED,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class BatchFailureRow:
    device_id: int
    device_name: str
    host: str
    cluster_name: str | None
    status: ScanTaskStatus
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class BatchFailurePage:
    batch_id: int
    batch_status: ScanBatchStatus
    total: int
    page: int
    page_size: int
    pages: int
    items: list[BatchFailureRow]


def _failure_filters(batch_id: int, query: str):
    filters = [
        ScanBatchItem.batch_id == batch_id,
        ScanBatchItem.status.in_(FAILED_BATCH_ITEM_STATUSES),
    ]
    normalized = query.strip()
    if normalized:
        pattern = f"%{normalized}%"
        filters.append(
            or_(
                Device.name.ilike(pattern),
                Device.host.ilike(pattern),
                Cluster.name.ilike(pattern),
                ScanTask.error_message.ilike(pattern),
            )
        )
    return filters


def list_batch_failures(
    session: Session,
    batch_id: int,
    page: int,
    page_size: int,
    query: str,
) -> BatchFailurePage:
    batch = session.get(ScanBatch, batch_id)
    if batch is None:
        raise LookupError("扫描批次不存在")

    filters = _failure_filters(batch_id, query)
    total = (
        session.scalar(
            select(func.count())
            .select_from(ScanBatchItem)
            .join(ScanTask, ScanBatchItem.task_id == ScanTask.id)
            .join(Device, ScanBatchItem.device_id == Device.id)
            .outerjoin(Cluster, Device.cluster_id == Cluster.id)
            .where(*filters)
        )
        or 0
    )
    rows = session.execute(
        select(
            ScanBatchItem.device_id,
            Device.name,
            Device.host,
            Cluster.name,
            ScanBatchItem.status,
            ScanTask.error_message,
            ScanTask.started_at,
            ScanTask.finished_at,
        )
        .join(ScanTask, ScanBatchItem.task_id == ScanTask.id)
        .join(Device, ScanBatchItem.device_id == Device.id)
        .outerjoin(Cluster, Device.cluster_id == Cluster.id)
        .where(*filters)
        .order_by(ScanTask.finished_at.desc().nullslast(), ScanBatchItem.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        BatchFailureRow(
            device_id=device_id,
            device_name=device_name,
            host=host,
            cluster_name=cluster_name,
            status=item_status,
            error_message=error_message
            or (
                "采集任务已取消"
                if item_status == ScanTaskStatus.CANCELLED
                else "采集任务发生内部错误"
            ),
            started_at=_as_utc(started_at),
            finished_at=_as_utc(finished_at),
        )
        for (
            device_id,
            device_name,
            host,
            cluster_name,
            item_status,
            error_message,
            started_at,
            finished_at,
        ) in rows
    ]
    return BatchFailurePage(
        batch_id=batch.id,
        batch_status=batch.status,
        total=total,
        page=page,
        page_size=page_size,
        pages=ceil(total / page_size) if total else 0,
        items=items,
    )


def failed_device_ids(session: Session, batch_id: int) -> list[int]:
    return list(
        session.scalars(
            select(ScanBatchItem.device_id)
            .where(
                ScanBatchItem.batch_id == batch_id,
                ScanBatchItem.status.in_(FAILED_BATCH_ITEM_STATUSES),
            )
            .distinct()
            .order_by(ScanBatchItem.device_id)
        )
    )
