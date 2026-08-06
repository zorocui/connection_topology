import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.collectors.base import CollectionResult, NormalizedConnection
from app.models import (
    ConnectionRecord,
    Device,
    OSType,
    ScanBatch,
    ScanBatchItem,
    ScanBatchStatus,
    ScanBatchType,
    ScanRun,
    ScanStatus,
    ScanTask,
    ScanTaskStatus,
    ScanTrigger,
)
from app.services.scan_queue import ScanQueueService
from app.services.scans import ScanOutcome
from app.services.task_leases import TaskLeaseLost, claim_scan_tasks


class BlockingCollector:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def test_connection(self, device, password):
        pass

    def collect(self, device, password):
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test collector was not released")
        return CollectionResult(())


def make_queue(
    app,
    *,
    worker_id: str,
    collector=None,
    heartbeat_seconds: float = 1,
    lease_seconds: float = 4,
) -> ScanQueueService:
    collector = collector or app.state.linux_collector
    queue = ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
        app.state.transaction_runner,
        max_workers=1,
        queue_size=10,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    queue.worker_id = worker_id
    return queue


def seed_device(app) -> int:
    with app.state.session_factory() as session:
        device = Device(
            name="lease-recovery-device",
            host="10.77.0.1",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(device)
        session.commit()
        return device.id


def successful_outcome(device_id: int) -> ScanOutcome:
    now = datetime.now(timezone.utc)
    return ScanOutcome(
        device_id=device_id,
        trigger=ScanTrigger.BATCH,
        status=ScanStatus.SUCCESS,
        started_at=now - timedelta(seconds=1),
        finished_at=now,
        connections=(
            NormalizedConnection(
                protocol="tcp",
                address_family="ipv4",
                local_ip="10.77.0.1",
                local_port=50000,
                remote_ip="203.0.113.20",
                remote_port=443,
                state="ESTABLISHED",
                pid=100,
                process_name="curl",
            ),
        ),
    )


def failed_outcome(device_id: int) -> ScanOutcome:
    now = datetime.now(timezone.utc)
    return ScanOutcome(
        device_id=device_id,
        trigger=ScanTrigger.BATCH,
        status=ScanStatus.FAILED,
        started_at=now - timedelta(seconds=1),
        finished_at=now,
        error_code="remote_failed",
        error_message="remote collection failed",
    )


def seed_expired_scan(app, worker_id: str = "worker-old") -> tuple[int, int, int]:
    device_id = seed_device(app)
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as session:
        batch = ScanBatch(
            batch_type=ScanBatchType.ALL,
            status=ScanBatchStatus.RUNNING,
            total_tasks=1,
            pending_tasks=0,
            running_tasks=1,
        )
        task = ScanTask(
            device_id=device_id,
            trigger_type=ScanTrigger.BATCH,
            priority=80,
            status=ScanTaskStatus.RUNNING,
            started_at=now - timedelta(minutes=2),
            worker_id=worker_id,
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(minutes=2),
            attempt_count=1,
        )
        session.add_all([batch, task])
        session.flush()
        session.add(
            ScanBatchItem(
                batch_id=batch.id,
                task_id=task.id,
                device_id=device_id,
                status=ScanTaskStatus.RUNNING,
            )
        )
        session.commit()
        return device_id, task.id, batch.id


@pytest.mark.parametrize("outcome_factory", [successful_outcome, failed_outcome])
def test_old_worker_cannot_persist_after_new_worker_reclaims_task(
    app,
    outcome_factory,
):
    device_id, task_id, batch_id = seed_expired_scan(app)
    claimed = app.state.transaction_runner.run(
        "reclaim_scan",
        lambda session: claim_scan_tasks(session, "worker-new", 1, 30, 90),
    )
    assert claimed == [task_id]

    old_queue = make_queue(app, worker_id="worker-old")
    with pytest.raises(TaskLeaseLost):
        app.state.transaction_runner.run(
            "persist_old_scan",
            lambda session: old_queue._persist_outcome(
                session,
                task_id,
                outcome_factory(device_id),
            ),
        )

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        device = session.get(Device, device_id)
        batch = session.get(ScanBatch, batch_id)
        item = session.scalar(
            select(ScanBatchItem).where(ScanBatchItem.task_id == task_id)
        )
        assert task.worker_id == "worker-new"
        assert task.status == ScanTaskStatus.RUNNING
        assert task.scan_run_id is None
        assert device.last_scan_status is None
        assert item.status == ScanTaskStatus.RUNNING
        assert batch.status == ScanBatchStatus.RUNNING
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 0
        assert session.scalar(select(func.count()).select_from(ConnectionRecord)) == 0


def test_old_worker_unexpected_failure_cannot_fail_reclaimed_task(app):
    _, task_id, _ = seed_expired_scan(app)
    claimed = app.state.transaction_runner.run(
        "reclaim_before_old_failure",
        lambda session: claim_scan_tasks(session, "worker-new", 1, 30, 90),
    )
    assert claimed == [task_id]

    old_queue = make_queue(app, worker_id="worker-old")
    with pytest.raises(TaskLeaseLost):
        old_queue._fail_unexpected_task(task_id)

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        assert task.worker_id == "worker-new"
        assert task.status == ScanTaskStatus.RUNNING
        assert task.error_message is None


def test_new_worker_recovers_crashed_task_and_clears_lease_on_success(app):
    device_id, task_id, batch_id = seed_expired_scan(app)
    queue = make_queue(app, worker_id="worker-new")
    assert queue._claim_next_task() == task_id

    app.state.transaction_runner.run(
        "persist_recovered_scan",
        lambda session: queue._persist_outcome(
            session,
            task_id,
            successful_outcome(device_id),
        ),
    )

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        batch = session.get(ScanBatch, batch_id)
        assert task.status == ScanTaskStatus.SUCCESS
        assert task.worker_id is None
        assert task.lease_expires_at is None
        assert task.heartbeat_at is None
        assert task.attempt_count == 2
        assert batch.status == ScanBatchStatus.COMPLETED
        assert batch.success_tasks == 1


def test_owned_failed_outcome_clears_lease_in_same_transaction(app):
    device_id = seed_device(app)
    queue = make_queue(app, worker_id="worker-failed")
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    assert task_id is not None

    app.state.transaction_runner.run(
        "persist_owned_failed_scan",
        lambda session: queue._persist_outcome(
            session,
            task_id,
            failed_outcome(device_id),
        ),
    )

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        persisted_batch = session.get(ScanBatch, batch.id)
        assert task.status == ScanTaskStatus.FAILED
        assert task.worker_id is None
        assert task.lease_expires_at is None
        assert task.heartbeat_at is None
        assert persisted_batch.status == ScanBatchStatus.COMPLETED
        assert persisted_batch.failed_tasks == 1


def test_application_passes_configured_scan_heartbeat_seconds(app):
    assert (
        app.state.scan_queue.task_heartbeat_seconds
        == app.state.settings.task_heartbeat_seconds
    )


def test_heartbeat_keeps_long_remote_collection_owned(app):
    collector = BlockingCollector()
    queue = make_queue(
        app,
        worker_id="worker-alive",
        collector=collector,
        heartbeat_seconds=0.2,
        lease_seconds=1,
    )
    device_id = seed_device(app)
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    queue.start()
    try:
        assert collector.started.wait(3)
        time.sleep(1.4)
        other_queue = make_queue(app, worker_id="worker-other")
        assert other_queue._claim_next_task() is None
        with app.state.session_factory() as session:
            task = session.scalar(select(ScanTask).where(ScanTask.device_id == device_id))
            database_now = session.scalar(select(func.now()))
            assert task.worker_id == "worker-alive"
            assert task.status == ScanTaskStatus.RUNNING
            assert task.lease_expires_at > database_now
    finally:
        collector.release.set()
        queue.shutdown()

    with app.state.session_factory() as session:
        assert session.get(ScanBatch, batch.id).status == ScanBatchStatus.COMPLETED


def test_shutdown_joins_scan_heartbeat_thread(app):
    queue = make_queue(app, worker_id="worker-shutdown", heartbeat_seconds=0.1)
    queue.start()
    heartbeat_thread = queue._heartbeat_thread
    assert heartbeat_thread is not None
    assert heartbeat_thread.is_alive()

    queue.shutdown()

    assert not heartbeat_thread.is_alive()
    assert queue._heartbeat_thread is None
