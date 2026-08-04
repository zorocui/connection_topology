import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.collectors.base import CollectionResult
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
from app.services.scan_queue import (
    DeviceCollectionDisabled,
    PRIORITY_MANUAL,
    PRIORITY_SCHEDULED,
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


def test_recovery_resets_running_tasks_and_batches(app):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app)
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    assert queue._claim_next_task() is not None

    assert queue.recover_running_tasks() == 1

    with app.state.session_factory() as session:
        task = session.scalar(select(ScanTask))
        recovered_batch = session.get(ScanBatch, batch.id)
        assert task.status == ScanTaskStatus.PENDING
        assert recovered_batch.status == ScanBatchStatus.PENDING
        assert recovered_batch.pending_tasks == 1


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


def test_worker_pool_reaches_configured_network_concurrency(app):
    device_ids = seed_devices(app, 30)
    collector = ConcurrencyCollector()
    queue = ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
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
        assert current.success_tasks == 30
        assert collector.maximum == 30
    finally:
        queue.shutdown()
