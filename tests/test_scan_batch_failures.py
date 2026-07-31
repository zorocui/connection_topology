from datetime import datetime, timezone

from app.models import (
    Cluster,
    Device,
    OSType,
    ScanBatch,
    ScanBatchItem,
    ScanBatchStatus,
    ScanBatchType,
    ScanTask,
    ScanTaskStatus,
    ScanTrigger,
)
from app.services.scan_batch_failures import (
    failed_device_ids,
    list_batch_failures,
)

NOW = datetime(2026, 7, 31, 4, 20, tzinfo=timezone.utc)


def add_batch_item(
    session,
    cipher,
    batch,
    *,
    index,
    status,
    error,
    cluster=None,
):
    device = Device(
        name=f"节点-{index}",
        host=f"10.20.0.{index}",
        os_type=OSType.LINUX,
        port=22,
        username="ops",
        encrypted_password=cipher.encrypt("secret"),
        cluster=cluster,
    )
    task = ScanTask(
        device=device,
        trigger_type=ScanTrigger.BATCH,
        priority=20,
        status=status,
        error_message=error,
        started_at=NOW,
        finished_at=NOW,
    )
    session.add_all([device, task])
    session.flush()
    session.add(
        ScanBatchItem(
            batch=batch,
            task=task,
            device_id=device.id,
            status=status,
        )
    )
    return device


def create_batch(session):
    batch = ScanBatch(
        batch_type=ScanBatchType.ALL,
        status=ScanBatchStatus.COMPLETED,
    )
    session.add(batch)
    session.flush()
    return batch


def test_lists_only_failed_items_with_pagination_and_stable_order(app):
    with app.state.session_factory() as session:
        batch = create_batch(session)
        for index, status in enumerate(
            [
                ScanTaskStatus.FAILED,
                ScanTaskStatus.FAILED,
                ScanTaskStatus.FAILED,
                ScanTaskStatus.CANCELLED,
                ScanTaskStatus.SUCCESS,
            ],
            start=1,
        ):
            add_batch_item(
                session,
                app.state.cipher,
                batch,
                index=index,
                status=status,
                error=f"错误-{index}",
            )
        session.commit()
        batch_id = batch.id

    with app.state.session_factory() as session:
        first = list_batch_failures(session, batch_id, 1, 2, "")
        second = list_batch_failures(session, batch_id, 2, 2, "")

    assert first.total == 4
    assert first.pages == 2
    assert len(first.items) == 2
    assert len(second.items) == 2
    assert {item.device_id for item in first.items}.isdisjoint(
        item.device_id for item in second.items
    )
    assert all(
        item.status in {ScanTaskStatus.FAILED, ScanTaskStatus.CANCELLED}
        for item in [*first.items, *second.items]
    )
    assert all(
        item.started_at.utcoffset().total_seconds() == 0
        for item in [*first.items, *second.items]
    )


def test_searches_name_host_cluster_and_error(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="核心集群")
        batch = create_batch(session)
        session.add(cluster)
        device = add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=8,
            status=ScanTaskStatus.FAILED,
            error="SSH 认证失败",
            cluster=cluster,
        )
        session.commit()
        batch_id, device_id = batch.id, device.id

    with app.state.session_factory() as session:
        for query in ("节点-8", "10.20.0.8", "核心集群", "认证失败"):
            page = list_batch_failures(session, batch_id, 1, 20, query)
            assert [item.device_id for item in page.items] == [device_id]


def test_failure_message_has_safe_fallback(app):
    with app.state.session_factory() as session:
        batch = create_batch(session)
        add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=1,
            status=ScanTaskStatus.FAILED,
            error=None,
        )
        add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=2,
            status=ScanTaskStatus.CANCELLED,
            error=None,
        )
        session.commit()
        batch_id = batch.id

    with app.state.session_factory() as session:
        page = list_batch_failures(session, batch_id, 1, 20, "")

    messages = {item.status: item.error_message for item in page.items}
    assert messages[ScanTaskStatus.FAILED] == "采集任务发生内部错误"
    assert messages[ScanTaskStatus.CANCELLED] == "采集任务已取消"


def test_failed_device_ids_are_unique_and_sorted(app):
    with app.state.session_factory() as session:
        batch = create_batch(session)
        second = add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=2,
            status=ScanTaskStatus.CANCELLED,
            error="已取消",
        )
        first = add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=1,
            status=ScanTaskStatus.FAILED,
            error="连接超时",
        )
        add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=3,
            status=ScanTaskStatus.SUCCESS,
            error=None,
        )
        session.commit()
        batch_id = batch.id
        expected = sorted([first.id, second.id])

    with app.state.session_factory() as session:
        assert failed_device_ids(session, batch_id) == expected


def test_missing_batch_raises_lookup_error(app):
    with app.state.session_factory() as session:
        try:
            list_batch_failures(session, 999999, 1, 20, "")
        except LookupError as exc:
            assert str(exc) == "扫描批次不存在"
        else:
            raise AssertionError("缺失批次应当抛出 LookupError")
