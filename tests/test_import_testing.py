import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import text

from app.collectors.base import CollectionResult, CollectorError
from app.models import (
    Device,
    ImportBatch,
    ImportBatchStatus,
    ImportRowResult,
    ImportStatus,
    ImportTestStatus,
    OSType,
    ScanBatch,
)
from app.services.import_testing import ImportTestService


class ImmediateExecutor:
    def submit(self, function, *args):
        function(*args)


@dataclass
class FakeCollector:
    error: Exception | None = None
    seen_devices: list = field(default_factory=list)

    def test_connection(self, device, password):
        self.seen_devices.append(device)
        if self.error:
            raise self.error

    def collect(self, device, password):
        return CollectionResult(())


class BarrierCollector:
    def __init__(self, participants):
        self.barrier = threading.Barrier(participants + 1)
        self.release = threading.Event()
        self.entered = 0
        self.lock = threading.Lock()

    def test_connection(self, device, password):
        with self.lock:
            self.entered += 1
        self.barrier.wait(timeout=10)
        if not self.release.wait(timeout=10):
            raise TimeoutError("测试未释放 collector")

    def collect(self, device, password):
        return CollectionResult(())


def seed_pending_row(app, host="10.0.0.10"):
    with app.state.session_factory() as session:
        device = Device(
            name=f"device-{host}",
            host=host,
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        batch = ImportBatch(
            filename="devices.xlsx",
            status=ImportBatchStatus.TESTING,
            total_rows=1,
            imported_rows=1,
            test_pending_rows=1,
        )
        session.add_all([device, batch])
        session.flush()
        row = ImportRowResult(
            batch_id=batch.id,
            row_number=2,
            device_name=device.name,
            host=device.host,
            device_id=device.id,
            import_status=ImportStatus.IMPORTED,
            import_message="导入成功",
            test_status=ImportTestStatus.PENDING,
        )
        session.add(row)
        session.commit()
        return batch.id, row.id, device.id


def seed_pending_rows(app, count):
    with app.state.session_factory() as session:
        batch = ImportBatch(
            filename="devices.xlsx",
            status=ImportBatchStatus.TESTING,
            total_rows=count,
            imported_rows=count,
            test_pending_rows=count,
        )
        session.add(batch)
        session.flush()
        for index in range(count):
            device = Device(
                name=f"pool-device-{index}",
                host=f"198.18.1.{index + 1}",
                os_type=OSType.LINUX,
                port=22,
                username="ops",
                encrypted_password=app.state.cipher.encrypt("secret"),
            )
            session.add(device)
            session.flush()
            session.add(
                ImportRowResult(
                    batch_id=batch.id,
                    row_number=index + 2,
                    device_name=device.name,
                    host=device.host,
                    device_id=device.id,
                    import_status=ImportStatus.IMPORTED,
                    import_message="导入成功",
                    test_status=ImportTestStatus.PENDING,
                )
            )
        session.commit()
        return batch.id


def test_background_import_test_success(app):
    batch_id, _, device_id = seed_pending_row(app)
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(),
        FakeCollector(),
    )
    service.schedule_batch(batch_id)
    with app.state.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.test_success_rows == 1
        assert session.get(Device, device_id) is not None


def test_import_test_passes_device_id_to_collector(app):
    batch_id, _, device_id = seed_pending_row(app, "10.0.0.19")
    collector = FakeCollector()
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        collector,
        FakeCollector(),
    )

    service.schedule_batch(batch_id)

    assert collector.seen_devices[0].device_id == device_id


def test_background_import_test_failure_keeps_device(app):
    batch_id, row_id, device_id = seed_pending_row(app, "10.0.0.11")
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(CollectorError("authentication_failed", "认证失败")),
        FakeCollector(),
    )
    service.resume_pending()
    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        batch = session.get(ImportBatch, batch_id)
        assert row.test_status == ImportTestStatus.FAILED
        assert batch.test_failed_rows == 1
        assert session.get(Device, device_id) is not None


def test_successful_import_creates_first_scan_batch(app):
    batch_id, _, device_id = seed_pending_row(app, "10.0.0.12")
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(),
        FakeCollector(),
        app.state.scan_queue.create_import_scan_batch,
    )

    service.schedule_batch(batch_id)
    service.resume_pending()

    with app.state.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        scan_batch = session.get(ScanBatch, batch.scan_batch_id)
        assert scan_batch is not None
        assert scan_batch.total_tasks == 1
        assert scan_batch.items[0].device_id == device_id


def test_import_first_scan_batch_excludes_not_applicable_marker(app):
    with app.state.session_factory() as session:
        batch = ImportBatch(
            filename="markers.xlsx",
            status=ImportBatchStatus.COMPLETED,
            total_rows=2,
            imported_rows=2,
        )
        normal = Device(
            name="normal",
            host="10.0.0.75",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        marker = Device(
            name="marker",
            host="10.0.0.76",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(""),
            collection_enabled=False,
        )
        session.add_all([batch, normal, marker])
        session.flush()
        session.add_all(
            [
                ImportRowResult(
                    batch_id=batch.id,
                    row_number=2,
                    device_id=normal.id,
                    import_status=ImportStatus.IMPORTED,
                    import_message="导入成功",
                    test_status=ImportTestStatus.SUCCESS,
                ),
                ImportRowResult(
                    batch_id=batch.id,
                    row_number=3,
                    device_id=marker.id,
                    import_status=ImportStatus.IMPORTED,
                    import_message="仅标注集群设备，不执行连接测试",
                    test_status=ImportTestStatus.NOT_APPLICABLE,
                ),
            ]
        )
        session.commit()
        batch_id = batch.id
        normal_id = normal.id

    scan_batch = app.state.scan_queue.create_import_scan_batch(batch_id)
    with app.state.session_factory() as session:
        persisted = session.get(ScanBatch, scan_batch.id)
        assert persisted.total_tasks == 1
        assert [item.device_id for item in persisted.items] == [normal_id]


def test_network_wait_does_not_hold_database_connections(app):
    count = 20
    batch_id = seed_pending_rows(app, count)
    collector = BarrierCollector(count)
    executor = ThreadPoolExecutor(max_workers=count)
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        executor,
        collector,
        collector,
    )
    try:
        service.schedule_batch(batch_id)
        collector.barrier.wait(timeout=10)
        assert collector.entered == count
        with app.state.session_factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
        collector.release.set()
    finally:
        collector.release.set()
        executor.shutdown(wait=True)

    with app.state.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch.test_pending_rows == 0
        assert batch.test_running_rows == 0
        assert batch.test_success_rows == count


def test_claimed_row_is_visible_as_running(app):
    batch_id, row_id, _ = seed_pending_row(app, "10.0.0.40")
    collector = BarrierCollector(1)
    executor = ThreadPoolExecutor(max_workers=1)
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        executor,
        collector,
        collector,
    )
    try:
        service.schedule_batch(batch_id)
        collector.barrier.wait(timeout=10)
        with app.state.session_factory() as session:
            row = session.get(ImportRowResult, row_id)
            batch = session.get(ImportBatch, batch_id)
            assert row.test_status == ImportTestStatus.RUNNING
            assert row.test_message == "正在测试连接"
            assert batch.test_pending_rows == 0
            assert batch.test_running_rows == 1
            assert batch.status == ImportBatchStatus.TESTING
    finally:
        collector.release.set()
        executor.shutdown(wait=True)


def test_resume_pending_recovers_stale_running_row(app):
    batch_id, row_id, _ = seed_pending_row(app, "10.0.0.41")
    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        row.test_status = ImportTestStatus.RUNNING
        row.test_message = "正在测试连接"
        batch = session.get(ImportBatch, batch_id)
        batch.test_pending_rows = 0
        batch.test_running_rows = 1
        session.commit()

    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(),
        FakeCollector(),
    )
    service.resume_pending()

    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        batch = session.get(ImportBatch, batch_id)
        assert row.test_status == ImportTestStatus.SUCCESS
        assert batch.test_pending_rows == 0
        assert batch.test_running_rows == 0
        assert batch.test_success_rows == 1
        assert batch.status == ImportBatchStatus.COMPLETED


def test_completed_row_is_not_overwritten(app):
    _, row_id, _ = seed_pending_row(app, "10.0.0.31")
    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        row.test_status = ImportTestStatus.FAILED
        row.test_message = "已有结果"
        session.commit()

    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(),
        FakeCollector(),
    )
    service.test_row(row_id)

    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        assert row.test_status == ImportTestStatus.FAILED
        assert row.test_message == "已有结果"


def test_row_deleted_during_network_test_is_safe(app):
    _, row_id, _ = seed_pending_row(app, "10.0.0.32")

    class DeleteRowCollector(FakeCollector):
        def test_connection(self, device, password):
            with app.state.session_factory() as session:
                row = session.get(ImportRowResult, row_id)
                session.delete(row)
                session.commit()

    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        DeleteRowCollector(),
        DeleteRowCollector(),
    )
    service.test_row(row_id)

    with app.state.session_factory() as session:
        assert session.get(ImportRowResult, row_id) is None


def test_device_deleted_during_network_test_is_not_restored(app):
    _, row_id, device_id = seed_pending_row(app, "10.0.0.33")

    class DeleteDeviceCollector(FakeCollector):
        def test_connection(self, device, password):
            with app.state.session_factory() as session:
                stored_device = session.get(Device, device_id)
                session.delete(stored_device)
                session.commit()

    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        DeleteDeviceCollector(),
        DeleteDeviceCollector(),
    )
    service.test_row(row_id)

    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        assert session.get(Device, device_id) is None
        assert row.device_id is None
        assert row.test_status == ImportTestStatus.SUCCESS


def test_batch_completed_callback_runs_once(app):
    batch_id, _, _ = seed_pending_row(app, "10.0.0.34")
    completed = []
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(),
        FakeCollector(),
        completed.append,
    )

    service.schedule_batch(batch_id)
    service.schedule_batch(batch_id)

    assert completed.count(batch_id) == 1


def test_future_database_exception_is_logged(app, caplog):
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(),
        FakeCollector(),
    )
    future = Future()
    future.set_exception(RuntimeError("database unavailable"))

    service._future_done(future)

    assert "导入连接测试后台任务异常" in caplog.text
    assert "database unavailable" in caplog.text


def test_import_testing_uses_application_write_coordinator(app, monkeypatch):
    names = []
    original = app.state.sqlite_write_coordinator.write

    def recording_write(name, operation):
        names.append(name)
        return original(name, operation)

    monkeypatch.setattr(app.state.sqlite_write_coordinator, "write", recording_write)
    _, row_id, _ = seed_pending_row(app, "10.0.0.88")
    app.state.import_test_service.test_row(row_id)
    assert "claim_import_test_row" in names
    assert "save_import_test_result" in names


def test_unexpected_row_error_is_persisted_as_failure(app, monkeypatch):
    batch_id, row_id, _ = seed_pending_row(app, "10.0.0.35")
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        FakeCollector(),
        FakeCollector(),
    )

    def fail_load(_row_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service, "_load_target", fail_load)
    service.test_row(row_id)

    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        batch = session.get(ImportBatch, batch_id)
        assert row.test_status == ImportTestStatus.FAILED
        assert "连接测试内部异常" in row.test_message
        assert "database unavailable" in row.test_message
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.test_pending_rows == 0
        assert batch.test_failed_rows == 1


def test_150_import_tests_complete_without_pool_timeout(app):
    count = 150
    batch_id = seed_pending_rows(app, count)
    executor = ThreadPoolExecutor(max_workers=20)
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        executor,
        FakeCollector(),
        FakeCollector(),
        app.state.scan_queue.create_import_scan_batch,
    )
    try:
        service.schedule_batch(batch_id)
    finally:
        executor.shutdown(wait=True)

    with app.state.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.test_pending_rows == 0
        assert batch.test_running_rows == 0
        assert batch.test_success_rows == count
        assert batch.test_failed_rows == 0
        assert batch.scan_batch_id is not None
        scan_batch = session.get(ScanBatch, batch.scan_batch_id)
        assert scan_batch.total_tasks == count


def test_1000_import_tests_complete_and_create_one_scan_batch(app):
    count = 1000
    batch_id = seed_pending_rows(app, count)
    executor = ThreadPoolExecutor(max_workers=20)
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        executor,
        FakeCollector(),
        FakeCollector(),
        app.state.scan_queue.create_import_scan_batch,
    )
    try:
        service.schedule_batch(batch_id)
    finally:
        executor.shutdown(wait=True)

    with app.state.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        first_scan_batch_id = batch.scan_batch_id
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.test_pending_rows == 0
        assert batch.test_running_rows == 0
        assert batch.test_success_rows == count
        assert batch.test_failed_rows == 0
        assert first_scan_batch_id is not None
        scan_batch = session.get(ScanBatch, first_scan_batch_id)
        assert scan_batch.total_tasks == count

    service.resume_pending()

    with app.state.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch.scan_batch_id == first_scan_batch_id
