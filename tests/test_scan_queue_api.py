import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.models import (
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


def create_device(client, payload, *, host, name, cluster_id=None):
    body = {**payload, "host": host, "name": name, "cluster_id": cluster_id}
    return client.post("/api/devices", json=body).json()


def wait_for_task(client, task_id):
    for _ in range(50):
        task = client.get(f"/api/scan-tasks/{task_id}").json()
        if task["status"] in {"success", "failed"}:
            return task
        time.sleep(0.02)
    raise AssertionError("扫描任务未在预期时间内结束")


def seed_api_batch(app, statuses):
    with app.state.session_factory() as session:
        batch = ScanBatch(
            batch_type=ScanBatchType.ALL,
            status=ScanBatchStatus.COMPLETED,
        )
        session.add(batch)
        session.flush()
        device_ids = []
        for index, (task_status, error) in enumerate(statuses, start=1):
            device = Device(
                name=f"接口节点-{index}",
                host=f"10.30.0.{index}",
                os_type=OSType.LINUX,
                port=22,
                username="ops",
                encrypted_password=app.state.cipher.encrypt("secret"),
            )
            task = ScanTask(
                device=device,
                trigger_type=ScanTrigger.BATCH,
                priority=20,
                status=task_status,
                error_message=error,
                started_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
                finished_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
            )
            session.add_all([device, task])
            session.flush()
            session.add(
                ScanBatchItem(
                    batch=batch,
                    task=task,
                    device_id=device.id,
                    status=task_status,
                )
            )
            device_ids.append(device.id)
        batch.total_tasks = len(statuses)
        batch.success_tasks = sum(
            status == ScanTaskStatus.SUCCESS for status, _ in statuses
        )
        batch.failed_tasks = sum(
            status in {ScanTaskStatus.FAILED, ScanTaskStatus.CANCELLED}
            for status, _ in statuses
        )
        session.commit()
        return batch.id, device_ids


def test_single_scan_is_asynchronous_and_pollable(client, linux_device_payload):
    device = create_device(
        client,
        linux_device_payload,
        host="10.0.1.1",
        name="async-one",
    )
    response = client.post(f"/api/devices/{device['id']}/scan")
    assert response.status_code == 202
    task = wait_for_task(client, response.json()["id"])
    assert task["status"] == "success"
    assert task["scan_run_id"] is not None


def test_all_device_batch_reports_progress(client, linux_device_payload):
    create_device(client, linux_device_payload, host="10.0.1.2", name="batch-one")
    create_device(client, linux_device_payload, host="10.0.1.3", name="batch-two")

    response = client.post("/api/scan-batches", json={"scope": "all"})
    assert response.status_code == 201
    batch_id = response.json()["id"]
    for _ in range(50):
        batch = client.get(f"/api/scan-batches/{batch_id}").json()
        if batch["status"] == "completed":
            break
        time.sleep(0.02)
    assert batch["total_tasks"] == 2
    assert batch["success_tasks"] == 2
    assert client.get("/api/scan-batches").json()[0]["id"] == batch_id


def test_cluster_batch_only_contains_cluster_devices(
    client, linux_device_payload
):
    cluster = client.post(
        "/api/clusters",
        json={"name": "api-cluster", "description": None},
    ).json()
    create_device(
        client,
        linux_device_payload,
        host="10.0.1.4",
        name="inside",
        cluster_id=cluster["id"],
    )
    create_device(client, linux_device_payload, host="10.0.1.5", name="outside")

    response = client.post(
        "/api/scan-batches",
        json={"scope": "cluster", "cluster_id": cluster["id"]},
    )
    assert response.status_code == 201
    assert response.json()["total_tasks"] == 1


def test_batch_failures_endpoint_supports_pagination_and_search(client, app):
    batch_id, _ = seed_api_batch(
        app,
        [
            (ScanTaskStatus.FAILED, "SSH 认证失败"),
            (ScanTaskStatus.FAILED, "连接超时"),
            (ScanTaskStatus.SUCCESS, None),
        ],
    )
    response = client.get(
        f"/api/scan-batches/{batch_id}/failures",
        params={"page": 1, "page_size": 20, "q": "认证"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["batch_id"] == batch_id
    assert payload["total"] == 1
    assert payload["items"][0]["error_message"] == "SSH 认证失败"


def test_batch_failures_endpoint_validates_batch_and_page(client):
    assert client.get("/api/scan-batches/999999/failures").status_code == 404
    assert (
        client.get("/api/scan-batches/999999/failures?page=0").status_code
        == 422
    )
    assert (
        client.get(
            "/api/scan-batches/999999/failures?page_size=101"
        ).status_code
        == 422
    )


def test_retry_failures_creates_new_retry_batch(client, app):
    app.state.scan_queue.shutdown()
    source_id, device_ids = seed_api_batch(
        app,
        [
            (ScanTaskStatus.FAILED, "连接超时"),
            (ScanTaskStatus.CANCELLED, "任务已取消"),
            (ScanTaskStatus.SUCCESS, None),
        ],
    )

    response = client.post(f"/api/scan-batches/{source_id}/retry-failures")

    assert response.status_code == 201
    retry = response.json()
    assert retry["id"] != source_id
    assert retry["batch_type"] == "retry"
    assert retry["total_tasks"] == 2
    with app.state.session_factory() as session:
        source = session.get(ScanBatch, source_id)
        retry_device_ids = session.scalars(
            select(ScanBatchItem.device_id)
            .where(ScanBatchItem.batch_id == retry["id"])
            .order_by(ScanBatchItem.device_id)
        ).all()
        assert source.failed_tasks == 2
        assert retry_device_ids == sorted(device_ids[:2])


def test_retry_failures_reuses_active_task(client, app):
    app.state.scan_queue.shutdown()
    source_id, device_ids = seed_api_batch(
        app,
        [(ScanTaskStatus.FAILED, "连接超时")],
    )
    with app.state.session_factory() as session:
        active = ScanTask(
            device_id=device_ids[0],
            trigger_type=ScanTrigger.MANUAL,
            priority=100,
            status=ScanTaskStatus.PENDING,
        )
        session.add(active)
        session.commit()
        active_id = active.id

    response = client.post(f"/api/scan-batches/{source_id}/retry-failures")

    assert response.status_code == 201
    with app.state.session_factory() as session:
        active_count = session.scalar(
            select(func.count())
            .select_from(ScanTask)
            .where(
                ScanTask.device_id == device_ids[0],
                ScanTask.status.in_(
                    [ScanTaskStatus.PENDING, ScanTaskStatus.RUNNING]
                ),
            )
        )
        item = session.scalar(
            select(ScanBatchItem).where(
                ScanBatchItem.batch_id == response.json()["id"]
            )
        )
        assert active_count == 1
        assert item.task_id == active_id


def test_retry_failures_rejects_batch_without_failures(client, app):
    source_id, _ = seed_api_batch(
        app,
        [(ScanTaskStatus.SUCCESS, None)],
    )

    response = client.post(f"/api/scan-batches/{source_id}/retry-failures")

    assert response.status_code == 409
    assert response.json()["detail"] == "该批次当前没有失败设备"
