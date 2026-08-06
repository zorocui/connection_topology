import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
from app.services.task_leases import claim_scan_tasks, renew_scan_leases


def seed_devices(app, count: int) -> list[int]:
    with app.state.session_factory() as session:
        devices = [
            Device(
                name=f"lease-server-{index}",
                host=f"10.20.0.{index + 1}",
                os_type=OSType.LINUX,
                port=22,
                username="ops",
                encrypted_password=app.state.cipher.encrypt("secret"),
            )
            for index in range(count)
        ]
        session.add_all(devices)
        session.commit()
        return [device.id for device in devices]


def seed_pending_tasks(app, device_ids: list[int]) -> list[int]:
    with app.state.session_factory() as session:
        tasks = [
            ScanTask(
                device_id=device_id,
                trigger_type=ScanTrigger.BATCH,
                priority=80,
                status=ScanTaskStatus.PENDING,
            )
            for device_id in device_ids
        ]
        session.add_all(tasks)
        session.commit()
        return [task.id for task in tasks]


def test_two_workers_never_claim_same_task_and_respect_global_limit(app):
    device_ids = seed_devices(app, 40)
    seed_pending_tasks(app, device_ids)
    barrier = threading.Barrier(2)

    def claim(worker_id: str) -> list[int]:
        barrier.wait()
        return app.state.transaction_runner.run(
            f"claim_{worker_id}",
            lambda session: claim_scan_tasks(session, worker_id, 30, 30, 90),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, worker_id) for worker_id in ("worker-a", "worker-b")]
        first, second = [future.result() for future in futures]

    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 30


def test_expired_task_is_requeued_and_claimed_by_new_worker(app):
    device_id = seed_devices(app, 1)[0]
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as session:
        task = ScanTask(
            device_id=device_id,
            trigger_type=ScanTrigger.BATCH,
            priority=80,
            status=ScanTaskStatus.RUNNING,
            started_at=now - timedelta(minutes=2),
            worker_id="dead-worker",
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(minutes=2),
            attempt_count=1,
        )
        session.add(task)
        session.commit()
        task_id = task.id

    claimed = app.state.transaction_runner.run(
        "claim_recovered",
        lambda session: claim_scan_tasks(session, "worker-new", 1, 30, 90),
    )

    assert claimed == [task_id]
    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        assert task is not None
        assert task.worker_id == "worker-new"
        assert task.status == ScanTaskStatus.RUNNING
        assert task.attempt_count == 2
        assert task.heartbeat_at is not None
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > task.heartbeat_at


def test_running_task_without_lease_is_recovered_instead_of_staying_stuck(app):
    device_id = seed_devices(app, 1)[0]
    with app.state.session_factory() as session:
        task = ScanTask(
            device_id=device_id,
            trigger_type=ScanTrigger.BATCH,
            priority=80,
            status=ScanTaskStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            worker_id="incomplete-worker",
            lease_expires_at=None,
            heartbeat_at=None,
            attempt_count=1,
        )
        session.add(task)
        session.commit()
        task_id = task.id

    claimed = app.state.transaction_runner.run(
        "claim_missing_lease",
        lambda session: claim_scan_tasks(session, "worker-new", 1, 30, 90),
    )

    assert claimed == [task_id]
    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        assert task is not None
        assert task.worker_id == "worker-new"
        assert task.attempt_count == 2
        assert task.lease_expires_at is not None


def test_expired_task_not_immediately_reclaimed_resets_its_batch(app):
    expired_device_id, pending_device_id = seed_devices(app, 2)
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as session:
        batch = ScanBatch(
            batch_type=ScanBatchType.ALL,
            status=ScanBatchStatus.RUNNING,
            total_tasks=1,
            pending_tasks=0,
            running_tasks=1,
        )
        expired_task = ScanTask(
            device_id=expired_device_id,
            trigger_type=ScanTrigger.BATCH,
            priority=20,
            status=ScanTaskStatus.RUNNING,
            worker_id="dead-worker",
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(minutes=2),
            attempt_count=1,
        )
        pending_task = ScanTask(
            device_id=pending_device_id,
            trigger_type=ScanTrigger.MANUAL,
            priority=100,
            status=ScanTaskStatus.PENDING,
        )
        session.add_all([batch, expired_task, pending_task])
        session.flush()
        session.add(
            ScanBatchItem(
                batch_id=batch.id,
                task_id=expired_task.id,
                device_id=expired_device_id,
                status=ScanTaskStatus.RUNNING,
            )
        )
        session.commit()
        batch_id = batch.id
        expired_task_id = expired_task.id
        pending_task_id = pending_task.id

    claimed = app.state.transaction_runner.run(
        "claim_higher_priority",
        lambda session: claim_scan_tasks(session, "worker-new", 1, 30, 90),
    )

    assert claimed == [pending_task_id]
    with app.state.session_factory() as session:
        expired_task = session.get(ScanTask, expired_task_id)
        item = session.scalar(
            select(ScanBatchItem).where(ScanBatchItem.batch_id == batch_id)
        )
        batch = session.get(ScanBatch, batch_id)
        assert expired_task is not None
        assert expired_task.status == ScanTaskStatus.PENDING
        assert expired_task.worker_id is None
        assert expired_task.lease_expires_at is None
        assert expired_task.heartbeat_at is None
        assert item is not None
        assert item.status == ScanTaskStatus.PENDING
        assert batch is not None
        assert batch.status == ScanBatchStatus.PENDING
        assert batch.pending_tasks == 1
        assert batch.running_tasks == 0


def test_unexpired_running_tasks_consume_application_wide_slots(app):
    device_ids = seed_devices(app, 35)
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as session:
        session.add_all(
            [
                ScanTask(
                    device_id=device_id,
                    trigger_type=ScanTrigger.BATCH,
                    priority=80,
                    status=ScanTaskStatus.RUNNING,
                    started_at=now,
                    worker_id="worker-existing",
                    lease_expires_at=now + timedelta(seconds=90),
                    heartbeat_at=now,
                    attempt_count=1,
                )
                for device_id in device_ids[:25]
            ]
        )
        session.add_all(
            [
                ScanTask(
                    device_id=device_id,
                    trigger_type=ScanTrigger.BATCH,
                    priority=80,
                    status=ScanTaskStatus.PENDING,
                )
                for device_id in device_ids[25:]
            ]
        )
        session.commit()

    claimed = app.state.transaction_runner.run(
        "claim_remaining_slots",
        lambda session: claim_scan_tasks(session, "worker-new", 30, 30, 90),
    )

    assert len(claimed) == 5
    with app.state.session_factory() as session:
        running = session.scalar(
            select(func.count()).select_from(ScanTask).where(
                ScanTask.status == ScanTaskStatus.RUNNING
            )
        )
        assert running == 30


def test_claim_sets_initial_lease_and_refreshes_linked_batch(app):
    device_id = seed_devices(app, 1)[0]
    batch = app.state.scan_queue.create_batch(ScanBatchType.ALL, [device_id])

    claimed = app.state.transaction_runner.run(
        "claim_with_batch",
        lambda session: claim_scan_tasks(session, "worker-batch", 1, 30, 90),
    )

    assert len(claimed) == 1
    with app.state.session_factory() as session:
        task = session.get(ScanTask, claimed[0])
        item = session.scalar(
            select(ScanBatchItem).where(ScanBatchItem.batch_id == batch.id)
        )
        persisted_batch = session.get(ScanBatch, batch.id)
        assert task is not None
        assert task.status == ScanTaskStatus.RUNNING
        assert task.worker_id == "worker-batch"
        assert task.started_at is not None
        assert task.heartbeat_at is not None
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > task.heartbeat_at
        assert task.attempt_count == 1
        assert item is not None
        assert item.status == ScanTaskStatus.RUNNING
        assert persisted_batch is not None
        assert persisted_batch.status == ScanBatchStatus.RUNNING
        assert persisted_batch.pending_tasks == 0
        assert persisted_batch.running_tasks == 1


def test_renew_scan_leases_updates_owned_tasks_and_returns_lost_ids(app):
    device_ids = seed_devices(app, 2)
    task_ids = seed_pending_tasks(app, device_ids)
    claimed = app.state.transaction_runner.run(
        "claim_for_renewal",
        lambda session: claim_scan_tasks(session, "worker-alive", 2, 30, 90),
    )
    assert set(claimed) == set(task_ids)

    with app.state.session_factory() as session:
        original_heartbeat = session.get(ScanTask, task_ids[0]).heartbeat_at

    lost = app.state.transaction_runner.run(
        "renew_scan_leases",
        lambda session: renew_scan_leases(
            session,
            "worker-alive",
            [task_ids[0], task_ids[1], 999_999],
            90,
        ),
    )

    assert lost == {999_999}
    with app.state.session_factory() as session:
        tasks = [session.get(ScanTask, task_id) for task_id in task_ids]
        assert all(task.worker_id == "worker-alive" for task in tasks)
        assert all(task.heartbeat_at >= original_heartbeat for task in tasks)
        assert all(task.lease_expires_at > task.heartbeat_at for task in tasks)


def test_renew_scan_leases_does_not_revive_expired_or_reassigned_task(app):
    first_device_id, second_device_id = seed_devices(app, 2)
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as session:
        expired = ScanTask(
            device_id=first_device_id,
            trigger_type=ScanTrigger.BATCH,
            priority=80,
            status=ScanTaskStatus.RUNNING,
            worker_id="worker-old",
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(seconds=10),
            attempt_count=1,
        )
        reassigned = ScanTask(
            device_id=second_device_id,
            trigger_type=ScanTrigger.BATCH,
            priority=80,
            status=ScanTaskStatus.RUNNING,
            worker_id="worker-new",
            lease_expires_at=now + timedelta(seconds=90),
            heartbeat_at=now,
            attempt_count=2,
        )
        session.add_all([expired, reassigned])
        session.commit()
        task_ids = [expired.id, reassigned.id]

    lost = app.state.transaction_runner.run(
        "renew_lost_scan_leases",
        lambda session: renew_scan_leases(
            session,
            "worker-old",
            task_ids,
            90,
        ),
    )

    assert lost == set(task_ids)
    with app.state.session_factory() as session:
        expired = session.get(ScanTask, task_ids[0])
        reassigned = session.get(ScanTask, task_ids[1])
        assert expired.lease_expires_at < datetime.now(timezone.utc)
        assert reassigned.worker_id == "worker-new"
