import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.collectors.base import CollectionResult, NormalizedConnection
from app.models import (
    ConnectionRecord,
    Device,
    ImportBatch,
    ImportBatchStatus,
    ImportRowResult,
    ImportStatus,
    ImportTestStatus,
    OSType,
    ScanBatch,
    ScanBatchStatus,
    ScanBatchType,
    ScanRun,
    ScanStatus,
    ScanTask,
    ScanTaskStatus,
)
from app.services.import_testing import ImportTestService
from app.services.scan_queue import ScanQueueService


@dataclass
class BarrierCollector:
    participants: int
    barrier: threading.Barrier = field(init=False)

    def __post_init__(self):
        self.barrier = threading.Barrier(self.participants)

    def test_connection(self, device, password):
        self.barrier.wait(timeout=15)

    def collect(self, device, password):
        self.barrier.wait(timeout=15)
        return CollectionResult(
            (
                NormalizedConnection(
                    protocol="tcp",
                    address_family="ipv4",
                    local_ip=device.host,
                    local_port=50000 + device.device_id,
                    remote_ip="203.0.113.10",
                    remote_port=443,
                    state="ESTABLISHED",
                    pid=device.device_id,
                    process_name="curl",
                ),
            )
        )


def seed_devices(app, count):
    with app.state.session_factory() as session:
        devices = [
            Device(
                name=f"concurrent-{index}",
                host=f"10.90.0.{index + 1}",
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


def make_queue(app, collector, workers):
    return ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
        app.state.transaction_runner,
        max_workers=workers,
        queue_size=200,
    )


def wait_for_batch(app, batch_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app.state.session_factory() as session:
            batch = session.get(ScanBatch, batch_id)
            if batch.status == ScanBatchStatus.COMPLETED:
                return
        time.sleep(0.05)
    raise AssertionError("扫描批次未在预期时间内完成")


def test_thirty_simultaneous_collections_persist_without_lock_failures(app):
    device_ids = seed_devices(app, 30)
    queue = make_queue(app, BarrierCollector(30), 30)
    batch = queue.create_batch(ScanBatchType.ALL, device_ids)
    queue.start()
    try:
        wait_for_batch(app, batch.id)
    finally:
        queue.shutdown()

    with app.state.session_factory() as session:
        persisted = session.get(ScanBatch, batch.id)
        assert persisted.total_tasks == 30
        assert persisted.success_tasks == 30
        assert persisted.failed_tasks == 0
        assert persisted.status == ScanBatchStatus.COMPLETED
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 30
        assert session.scalar(select(func.count()).select_from(ConnectionRecord)) == 30
        assert session.scalar(
            select(func.count()).select_from(Device).where(
                Device.last_scan_status == ScanStatus.SUCCESS
            )
        ) == 30
        assert session.scalar(
            select(func.count()).select_from(ScanTask).where(
                ScanTask.status == ScanTaskStatus.SUCCESS
            )
        ) == 30


class RetryableDriverError(Exception):
    sqlstate = "40001"


def transient_transaction_error():
    return OperationalError("INSERT INTO scan_runs", (), RetryableDriverError())


def test_persist_retry_rebuilds_rows_without_duplicates(app, monkeypatch):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app, BarrierCollector(1), 1)
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    original_write = app.state.transaction_runner.run
    persist_attempts = 0

    def flaky_write(name, operation):
        if name != "persist_scan":
            return original_write(name, operation)

        def flaky_operation(session):
            nonlocal persist_attempts
            persist_attempts += 1
            result = operation(session)
            if persist_attempts < 3:
                raise transient_transaction_error()
            return result

        return original_write(name, flaky_operation)

    monkeypatch.setattr(app.state.transaction_runner, "run", flaky_write)
    queue._execute_task(task_id)

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        assert persist_attempts == 3
        assert task.status == ScanTaskStatus.SUCCESS
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 1
        assert session.scalar(select(func.count()).select_from(ConnectionRecord)) == 1
        assert session.get(ScanBatch, batch.id).success_tasks == 1


def test_import_test_and_scan_finish_together_without_internal_errors(app):
    collector = BarrierCollector(2)
    device_ids = seed_devices(app, 2)
    queue = make_queue(app, collector, 1)
    scan_batch = queue.create_batch(ScanBatchType.ALL, [device_ids[0]])

    with (
        app.state.transaction_runner.guard("seed_import_test"),
        app.state.session_factory() as session,
    ):
        import_batch = ImportBatch(
                filename="mixed.xlsx",
                status=ImportBatchStatus.TESTING,
                total_rows=1,
                imported_rows=1,
                test_pending_rows=1,
            )
        session.add(import_batch)
        session.flush()
        row = ImportRowResult(
                batch_id=import_batch.id,
                row_number=2,
                device_id=device_ids[1],
                import_status=ImportStatus.IMPORTED,
                import_message="导入成功，等待连接测试",
                test_status=ImportTestStatus.PENDING,
            )
        session.add(row)
        session.commit()
        row_id = row.id

    executor = ThreadPoolExecutor(max_workers=1)
    import_service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        executor,
        collector,
        collector,
        app.state.transaction_runner,
    )
    queue.start()
    try:
        import_service._submit(row_id)
        wait_for_batch(app, scan_batch.id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with app.state.session_factory() as session:
                row = session.get(ImportRowResult, row_id)
                if row.test_status == ImportTestStatus.SUCCESS:
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("导入连接测试未在预期时间内完成")
    finally:
        queue.shutdown()
        executor.shutdown(wait=True)

    with app.state.session_factory() as session:
        task = session.scalar(
            select(ScanTask).where(ScanTask.device_id == device_ids[0])
        )
        row = session.get(ImportRowResult, row_id)
        assert task.status == ScanTaskStatus.SUCCESS
        assert row.test_status == ImportTestStatus.SUCCESS
        combined = f"{task.error_message or ''} {row.test_message or ''}"
        assert "internal_error" not in combined
        assert "database is locked" not in combined
        assert "UPDATE devices" not in combined
