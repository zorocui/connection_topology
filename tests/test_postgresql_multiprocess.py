from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.request import urlopen

import pytest
from sqlalchemy import select

from app.collectors.base import CollectionResult
from app.models import (
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
)
from app.services.import_testing import ImportTestService
from app.services.scan_queue import ScanQueueService
from tests.conftest import _load_test_database_url


@dataclass
class BlockingCollector:
    first_wave: int
    first_wave_ready: threading.Event = field(default_factory=threading.Event)
    release_first_wave: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    entered: int = 0
    maximum: int = 0

    def _block(self) -> None:
        with self.lock:
            self.active += 1
            self.entered += 1
            self.maximum = max(self.maximum, self.active)
            if self.entered == self.first_wave:
                self.first_wave_ready.set()
        try:
            if not self.release_first_wave.wait(20):
                raise TimeoutError("并发测试未释放首批采集器")
        finally:
            with self.lock:
                self.active -= 1

    def test_connection(self, device, password: str) -> None:
        self._block()

    def collect(self, device, password: str) -> CollectionResult:
        self._block()
        return CollectionResult(())


def seed_devices(app, count: int) -> list[int]:
    with app.state.session_factory() as session:
        devices = [
            Device(
                name=f"global-scan-{index}",
                host=f"10.91.0.{index + 1}",
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


def make_queue(app, collector: BlockingCollector, worker_id: str) -> ScanQueueService:
    queue = ScanQueueService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
        app.state.transaction_runner,
        max_workers=30,
        queue_size=200,
    )
    queue.worker_id = worker_id
    return queue


def wait_for_scan_batch(app, batch_id: int, timeout: float = 30) -> ScanBatch:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app.state.session_factory() as session:
            batch = session.get(ScanBatch, batch_id)
            if batch.status == ScanBatchStatus.COMPLETED:
                session.expunge(batch)
                return batch
        time.sleep(0.05)
    raise AssertionError(f"scan batch {batch_id} did not complete")


def test_two_queue_instances_share_global_thirty_scan_limit(app):
    tracker = BlockingCollector(first_wave=30)
    device_ids = seed_devices(app, 60)
    first = make_queue(app, tracker, "process-a")
    second = make_queue(app, tracker, "process-b")
    batch = first.create_batch(ScanBatchType.ALL, device_ids)
    first.start()
    second.start()
    try:
        assert tracker.first_wave_ready.wait(15)
        assert tracker.maximum == 30
        tracker.release_first_wave.set()
        persisted = wait_for_scan_batch(app, batch.id)
    finally:
        tracker.release_first_wave.set()
        first.shutdown()
        second.shutdown()
    assert tracker.maximum == 30
    assert persisted.success_tasks == 60


def seed_import_rows(app, count: int) -> int:
    with app.state.session_factory() as session:
        batch = ImportBatch(
            filename="multiprocess.xlsx",
            status=ImportBatchStatus.TESTING,
            total_rows=count,
            imported_rows=count,
            test_pending_rows=count,
        )
        session.add(batch)
        session.flush()
        for index in range(count):
            device = Device(
                name=f"global-import-{index}",
                host=f"198.19.0.{index + 1}",
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
                    device_id=device.id,
                    device_name=device.name,
                    host=device.host,
                    import_status=ImportStatus.IMPORTED,
                    import_message="导入成功",
                    test_status=ImportTestStatus.PENDING,
                )
            )
        session.commit()
        return batch.id


def wait_for_import_batch(app, batch_id: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app.state.session_factory() as session:
            if session.get(ImportBatch, batch_id).status == ImportBatchStatus.COMPLETED:
                return
        time.sleep(0.05)
    raise AssertionError(f"import batch {batch_id} did not complete")


def make_import_service(app, collector: BlockingCollector, worker_id: str) -> ImportTestService:
    return ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        None,
        collector,
        collector,
        app.state.transaction_runner,
        max_workers=20,
        global_limit=20,
        worker_id=worker_id,
    )


def test_two_import_services_share_global_twenty_limit(app):
    batch_id = seed_import_rows(app, 40)
    tracker = BlockingCollector(first_wave=20)
    first = make_import_service(app, tracker, "import-process-a")
    second = make_import_service(app, tracker, "import-process-b")
    first.start()
    second.start()
    try:
        assert tracker.first_wave_ready.wait(15)
        assert tracker.maximum == 20
        tracker.release_first_wave.set()
        wait_for_import_batch(app, batch_id)
    finally:
        tracker.release_first_wave.set()
        first.shutdown()
        second.shutdown()
    with app.state.session_factory() as session:
        rows = session.scalars(
            select(ImportRowResult).where(ImportRowResult.batch_id == batch_id)
        ).all()
    assert tracker.maximum == 20
    assert len(rows) == 40
    assert all(row.test_status == ImportTestStatus.SUCCESS for row in rows)
    assert all(row.test_attempt_count == 1 for row in rows)


def _stop_process_tree(process: subprocess.Popen) -> str:
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            process.terminate()
    try:
        output, _ = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=5)
    return output


@pytest.mark.skipif(os.name != "nt", reason="Windows production startup smoke test")
def test_real_two_worker_uvicorn_health_smoke(test_database_url, valid_key):
    environment = os.environ.copy()
    environment.update(
        DATABASE_URL=test_database_url,
        APP_SECRET_KEY=valid_key,
        SCHEDULER_ENABLED="false",
        DB_POOL_SIZE="2",
        DB_MAX_OVERFLOW="0",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--workers",
            "2",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = ""
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urlopen("http://127.0.0.1:8765/api/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.25)
        else:
            raise AssertionError("two-worker Uvicorn did not become healthy")

        def health_request(_: int) -> bytes:
            with urlopen("http://127.0.0.1:8765/api/health", timeout=3) as response:
                assert response.status == 200
                return response.read()

        with ThreadPoolExecutor(max_workers=20) as executor:
            bodies = list(executor.map(health_request, range(20)))
        assert all(b'"database":"ok"' in body for body in bodies)
        assert all(b'"migration":"current"' in body for body in bodies)
    finally:
        output = _stop_process_tree(process)
    lowered = output.lower()
    assert "migration race" not in lowered
    assert "duplicate scheduler" not in lowered


def test_missing_test_database_url_names_required_variable(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL"):
        _load_test_database_url(env_file=None)
