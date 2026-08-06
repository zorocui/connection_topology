import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.collectors.base import CollectionResult
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
from app.services.database_transactions import (
    TRANSACTION_CONFLICT_MESSAGE,
    TransactionConflict,
)
from app.services.scan_queue import (
    PRIORITY_MANUAL,
    PRIORITY_SCHEDULED,
    DeviceCollectionDisabled,
    ScanQueueFull,
    ScanQueueService,
)


def make_queue(
    app,
    *,
    max_workers=2,
    queue_size=20,
    on_successful_scan=None,
):
    return ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        app.state.linux_collector,
        app.state.windows_collector,
        app.state.transaction_runner,
        max_workers=max_workers,
        queue_size=queue_size,
        on_successful_scan=on_successful_scan,
    )


def seed_devices(app, count):
    with app.state.session_factory() as session:
        devices = [
            Device(
                name=f"server-{index}",
                host=f"10.0.0.{index + 1}",
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


def test_execute_persists_run_device_task_items_and_batch_atomically(app):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app)
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()

    queue._execute_task(task_id)

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        run = session.get(ScanRun, task.scan_run_id)
        item = session.scalar(
            select(ScanBatchItem).where(ScanBatchItem.batch_id == batch.id)
        )
        persisted_batch = session.get(ScanBatch, batch.id)
        device = session.get(Device, device_id)
        assert task.status == ScanTaskStatus.SUCCESS
        assert run.status == ScanStatus.SUCCESS
        assert device.last_scan_status == ScanStatus.SUCCESS
        assert item.status == ScanTaskStatus.SUCCESS
        assert persisted_batch.success_tasks == 1
        assert persisted_batch.status == ScanBatchStatus.COMPLETED


def test_claims_enqueue_and_batch_mutations_use_shared_transaction_runner(app, monkeypatch):
    calls = []
    original = app.state.transaction_runner.run

    def recording_write(name, operation):
        calls.append(name)
        return original(name, operation)

    monkeypatch.setattr(app.state.transaction_runner, "run", recording_write)
    device_id = seed_devices(app, 1)[0]
    queue = app.state.scan_queue
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    queue.cancel_device(device_id)
    assert {"create_scan_batch", "claim_scan_tasks", "cancel_scan_device"} <= set(calls)
    assert batch.id
    assert task_id


def test_persist_retry_exhaustion_records_transaction_conflict(app, monkeypatch):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app)
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    original = queue.transaction_runner.run

    def fail_persist(name, operation):
        if name == "persist_scan":
            raise TransactionConflict(name)
        return original(name, operation)

    monkeypatch.setattr(queue.transaction_runner, "run", fail_persist)
    queue._execute_safely(task_id)

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        run = session.get(ScanRun, task.scan_run_id)
        persisted_batch = session.get(ScanBatch, batch.id)
        assert task.status == ScanTaskStatus.FAILED
        assert task.error_message == TRANSACTION_CONFLICT_MESSAGE
        assert run.error_code == "transaction_conflict"
        assert run.error_message == TRANSACTION_CONFLICT_MESSAGE
        assert session.scalar(
            select(func.count()).select_from(ConnectionRecord).where(
                ConnectionRecord.scan_run_id == run.id
            )
        ) == 0
        assert persisted_batch.failed_tasks == 1


def seed_marker(app):
    with app.state.session_factory() as session:
        marker = Device(
            name="marker",
            host="10.0.9.90",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(""),
            collection_enabled=False,
        )
        session.add(marker)
        session.commit()
        return marker.id


def test_marker_cannot_be_enqueued_directly(app):
    marker_id = seed_marker(app)
    queue = make_queue(app)
    with pytest.raises(DeviceCollectionDisabled, match="仅用于集群标注"):
        queue.enqueue_device(marker_id, ScanTrigger.MANUAL, PRIORITY_MANUAL)


def test_batch_excludes_marker_device(app):
    normal_id = seed_devices(app, 1)[0]
    marker_id = seed_marker(app)
    queue = make_queue(app)
    batch = queue.create_batch(ScanBatchType.ALL, [normal_id, marker_id])
    with app.state.session_factory() as session:
        persisted = session.get(ScanBatch, batch.id)
        assert [item.device_id for item in persisted.items] == [normal_id]


def test_duplicate_device_reuses_task_and_raises_priority(app):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app)

    first = queue.enqueue_device(device_id, ScanTrigger.SCHEDULED, PRIORITY_SCHEDULED)
    second = queue.enqueue_device(device_id, ScanTrigger.MANUAL, PRIORITY_MANUAL)

    assert second.id == first.id
    assert second.priority == PRIORITY_MANUAL
    assert second.trigger_type == ScanTrigger.MANUAL


def test_capacity_counts_unique_active_tasks(app):
    first_id, second_id = seed_devices(app, 2)
    queue = make_queue(app, queue_size=1)
    queue.enqueue_device(first_id, ScanTrigger.MANUAL, PRIORITY_MANUAL)

    with pytest.raises(ScanQueueFull, match="扫描队列已满"):
        queue.enqueue_device(second_id, ScanTrigger.MANUAL, PRIORITY_MANUAL)


def test_active_task_can_belong_to_two_batches(app):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app)
    first = queue.create_batch(ScanBatchType.ALL, [device_id])
    second = queue.create_batch(ScanBatchType.CLUSTER, [device_id], cluster_id=None)

    with app.state.session_factory() as session:
        tasks = session.scalars(select(ScanTask)).all()
        items = session.scalars(select(ScanBatchItem)).all()
        assert len(tasks) == 1
        assert {item.batch_id for item in items} == {first.id, second.id}


def test_claims_are_unique_across_threads(app):
    device_ids = seed_devices(app, 30)
    queue = make_queue(app, max_workers=30, queue_size=30)
    queue.create_batch(ScanBatchType.ALL, device_ids)

    with ThreadPoolExecutor(max_workers=30) as executor:
        claimed = list(executor.map(lambda _: queue._claim_next_task(), range(30)))

    assert None not in claimed
    assert len(set(claimed)) == 30


def test_two_queue_instances_share_application_wide_scan_limit(app):
    device_ids = seed_devices(app, 40)
    first_queue = make_queue(app, max_workers=30, queue_size=40)
    second_queue = make_queue(app, max_workers=30, queue_size=40)
    first_queue.create_batch(ScanBatchType.ALL, device_ids)
    barrier = threading.Barrier(2)

    def claim(queue):
        barrier.wait()
        return queue._claim_tasks(30)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, queue) for queue in (first_queue, second_queue)]
        first, second = [future.result() for future in futures]

    assert first_queue.worker_id != second_queue.worker_id
    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 30


def test_execute_task_settles_all_linked_batches(app):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app)
    first = queue.create_batch(ScanBatchType.ALL, [device_id])
    second = queue.create_batch(ScanBatchType.CLUSTER, [device_id])
    task_id = queue._claim_next_task()
    assert task_id is not None

    queue._execute_task(task_id)

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        batches = [session.get(ScanBatch, batch_id) for batch_id in (first.id, second.id)]
        assert task.status == ScanTaskStatus.SUCCESS
        assert all(batch.status == ScanBatchStatus.COMPLETED for batch in batches)
        assert all(batch.success_tasks == 1 for batch in batches)


def test_successful_scan_invokes_topology_cache_invalidation(app):
    device_id = seed_devices(app, 1)[0]
    invalidations = []
    queue = make_queue(
        app,
        on_successful_scan=lambda: invalidations.append("cleared"),
    )
    queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    assert task_id is not None

    queue._execute_task(task_id)

    assert invalidations == ["cleared"]


class ConcurrencyCollector:
    def __init__(self, participants=30):
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(participants)
        self.active = 0
        self.maximum = 0

    def test_connection(self, device, password):
        pass

    def collect(self, device, password):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        self.barrier.wait(timeout=3)
        with self.lock:
            self.active -= 1
        return CollectionResult(())


class BlockingConcurrencyCollector:
    def __init__(self, target_concurrency=30):
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.target_reached = threading.Event()
        self.target_concurrency = target_concurrency
        self.active = 0
        self.maximum = 0
        self.device_ids = []

    def test_connection(self, device, password):
        pass

    def collect(self, device, password):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.device_ids.append(device.device_id)
            if self.active >= self.target_concurrency:
                self.target_reached.set()
        self.release.wait(timeout=10)
        with self.lock:
            self.active -= 1
        return CollectionResult(())


def test_two_dispatchers_never_exceed_global_network_concurrency(app):
    device_ids = seed_devices(app, 40)
    collector = BlockingConcurrencyCollector()
    first_queue = ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
        app.state.transaction_runner,
        max_workers=30,
        queue_size=100,
    )
    second_queue = ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
        app.state.transaction_runner,
        max_workers=30,
        queue_size=100,
    )
    first_queue.create_batch(ScanBatchType.ALL, device_ids)
    first_queue.start()
    second_queue.start()
    try:
        assert collector.target_reached.wait(timeout=10)
        with app.state.session_factory() as session:
            running = session.scalar(
                select(func.count()).select_from(ScanTask).where(
                    ScanTask.status == ScanTaskStatus.RUNNING
                )
            )
            pending = session.scalar(
                select(func.count()).select_from(ScanTask).where(
                    ScanTask.status == ScanTaskStatus.PENDING
                )
            )
        assert running == 30
        assert pending == 10
        assert collector.maximum == 30
        assert len(collector.device_ids) == 30
        assert len(set(collector.device_ids)) == 30
    finally:
        collector.release.set()
        first_queue.shutdown()
        second_queue.shutdown()


def test_worker_pool_reaches_configured_network_concurrency(app):
    device_ids = seed_devices(app, 30)
    collector = ConcurrencyCollector()
    queue = ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
        app.state.transaction_runner,
        max_workers=30,
        queue_size=100,
    )
    batch = queue.create_batch(ScanBatchType.ALL, device_ids)
    queue.start()
    try:
        for _ in range(100):
            with app.state.session_factory() as session:
                current = session.get(ScanBatch, batch.id)
                if current.status == ScanBatchStatus.COMPLETED:
                    break
            time.sleep(0.05)
        assert current.status == ScanBatchStatus.COMPLETED
        assert current.total_tasks == 30
        assert current.pending_tasks == 0
        assert current.running_tasks == 0
        assert current.success_tasks == 30
        assert current.failed_tasks == 0
        assert collector.maximum == 30
    finally:
        queue.shutdown()


def test_concurrent_request_owned_device_deletes_use_one_connection_each(app):
    device_ids = seed_devices(app, 10)
    queue = app.state.scan_queue

    def delete_device_in_request(device_id):
        with (
            app.state.transaction_runner.guard("api_delete_device"),
            app.state.session_factory() as session,
        ):
            device = session.get(Device, device_id)
            assert device is not None
            assert queue.cancel_device(device_id, session=session) is True
            session.delete(device)
            session.commit()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(delete_device_in_request, device_id) for device_id in device_ids]
        for future in futures:
            future.result()

    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Device)) == 0
