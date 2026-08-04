# SQLite Concurrent Scan Write Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve 30-way remote collection concurrency while making all SQLite writes coordinated, retryable, transactionally consistent, correctly classified, and safe from accidental multi-process startup.

**Architecture:** Introduce one application-scoped `SQLiteWriteCoordinator` that serializes write transactions and replays retryable operations with fresh sessions. Split remote collection from persistence so the queue atomically saves the scan run, connection rows, device state, task state, and batch counts in one coordinated transaction. Add an OS-level SQLite process guard so a second Uvicorn process fails before starting duplicate background services.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy 2, SQLite WAL, APScheduler, Paramiko/WinRM collectors, Pydantic Settings, pytest, Ruff; standard library `threading`, `msvcrt` on Windows, and `fcntl` on POSIX.

## Global Constraints

- Keep `SCAN_MAX_WORKERS=30`; it remains the remote SSH/WinRM collection concurrency, not database write concurrency.
- Keep `SQLITE_BUSY_TIMEOUT_MS=30000` as the SQLite driver/PRAGMA wait layer.
- Default `SQLITE_WRITE_RETRY_DELAYS` is exactly `0.1,0.3,0.8,1.5,3` seconds.
- SQLite file mode supports exactly one Uvicorn application process; a second process must fail startup with a clear explanation.
- Do not add PostgreSQL, Redis, an external queue, a distributed lock, or a new runtime dependency.
- Never hold the coordinator lock during SSH/WinRM connection, command execution, or result parsing.
- Preserve the existing per-device nonblocking scan lock so the same device cannot be collected twice concurrently.
- A scan persistence transaction atomically writes `ScanRun`, `ConnectionRecord`, `Device`, `ScanTask`, `ScanBatchItem`, and affected `ScanBatch` counters.
- Retryable SQLite errors are only `OperationalError` messages containing `database ... locked` or `database ... busy`.
- Retry exhaustion is represented by exact code `database_busy` and exact message `数据库繁忙，扫描结果未能保存，请重试`.
- Retry logs contain operation name and attempt count only; never SQL parameters, passwords, or encrypted passwords.
- Existing collector error codes, credential redaction, import behavior, cluster marker behavior, and topology behavior remain unchanged.

---

## File Map

- Create `app/services/sqlite_writes.py`: transient-error detection, `DatabaseBusy`, shared reentrant write coordinator, fresh-session retries.
- Create `app/services/process_guard.py`: cross-platform SQLite file process guard and URL-to-file resolution.
- Modify `app/config.py`: parse and validate SQLite retry delays.
- Modify `app/main.py`: construct/inject coordinator, acquire/release process guard, coordinate startup writes.
- Modify `app/services/scans.py`: immutable scan target/outcome collection and session-independent persistence helper.
- Modify `app/services/scan_queue.py`: coordinated queue mutations and atomic scan outcome persistence.
- Modify `app/services/import_testing.py`: replace private lock/retry loop with shared coordinator.
- Modify `app/services/imports.py`: enter the shared write boundary before every explicit flush/commit.
- Modify `app/services/scheduler.py`: coordinate purge writes and preserve network-independent scheduling.
- Modify `app/routes/api.py`: coordinate mutating request transactions and map `DatabaseBusy` to HTTP 503.
- Modify `README.md`: document 30-way thread concurrency, single-process guard, and the new retry setting.
- Test `tests/test_sqlite_writes.py`, `tests/test_process_guard.py`, `tests/test_services.py`, `tests/test_scan_queue.py`, `tests/test_scan_queue_api.py`, `tests/test_import_testing.py`, `tests/test_imports.py`, `tests/test_scheduler_queue.py`, `tests/test_api.py`, `tests/test_config.py`, `tests/test_main.py`.

### Task 1: Shared SQLite write coordinator and retry configuration

**Files:**
- Create: `app/services/sqlite_writes.py`
- Modify: `app/config.py`
- Test: `tests/test_sqlite_writes.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `is_transient_sqlite_write_error(exc: Exception) -> bool`.
- Produces: `DatabaseBusy(operation_name: str)` with user-safe string `数据库繁忙，扫描结果未能保存，请重试`.
- Produces: `SQLiteWriteCoordinator(session_factory, retry_delays, enabled=True)`.
- Produces: `write(operation_name: str, operation: Callable[[Session], T]) -> T`.
- Produces: `write_once(operation_name: str) -> ContextManager[None]`.
- Produces: `Settings.sqlite_write_retry_delays: tuple[float, ...]`.

- [ ] **Step 1: Add failing configuration tests**

Extend `tests/test_config.py`:

```python
def test_sqlite_write_retry_delays_default_and_env(valid_key, monkeypatch):
    default = Settings(app_secret_key=valid_key, _env_file=None)
    assert default.sqlite_write_retry_delays == (0.1, 0.3, 0.8, 1.5, 3.0)

    monkeypatch.setenv("SQLITE_WRITE_RETRY_DELAYS", "0,0.25,2")
    configured = Settings(app_secret_key=valid_key, _env_file=None)
    assert configured.sqlite_write_retry_delays == (0.0, 0.25, 2.0)


@pytest.mark.parametrize("value", ["", "-1,0.2", "abc", ",,,"])
def test_sqlite_write_retry_delays_reject_invalid_values(
    valid_key,
    monkeypatch,
    value,
):
    monkeypatch.setenv("SQLITE_WRITE_RETRY_DELAYS", value)
    with pytest.raises(ValidationError):
        Settings(app_secret_key=valid_key, _env_file=None)
```

Add `ValidationError` from `pydantic` and `Settings` from `app.config` to the existing imports.

- [ ] **Step 2: Add failing coordinator tests**

Create `tests/test_sqlite_writes.py`:

```python
import logging
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.models import SystemSetting
from app.services.sqlite_writes import (
    DatabaseBusy,
    SQLiteWriteCoordinator,
    is_transient_sqlite_write_error,
)


def locked_error(message="database is locked"):
    return OperationalError("UPDATE devices SET name=?", (), Exception(message))


@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_transient_detection_accepts_only_sqlite_lock_messages(message):
    assert is_transient_sqlite_write_error(locked_error(message)) is True
    assert is_transient_sqlite_write_error(ValueError(message)) is False
    assert is_transient_sqlite_write_error(locked_error("disk I/O error")) is False


def test_write_retries_with_fresh_sessions_and_commits(app, monkeypatch, caplog):
    coordinator = SQLiteWriteCoordinator(
        app.state.session_factory,
        retry_delays=(0.0, 0.0),
    )
    session_ids = []
    attempts = 0

    def operation(session):
        nonlocal attempts
        attempts += 1
        session_ids.append(id(session))
        if attempts < 3:
            raise locked_error()
        session.merge(SystemSetting(id=1, history_retention_days=9))
        return "saved"

    with caplog.at_level(logging.WARNING, logger="app.services.sqlite_writes"):
        assert coordinator.write("test_write", operation) == "saved"

    assert attempts == 3
    assert len(set(session_ids)) == 3
    assert "operation=test_write" in caplog.text
    assert "UPDATE system_settings" not in caplog.text
    with app.state.session_factory() as session:
        assert session.execute(
            text("SELECT history_retention_days FROM system_settings WHERE id=1")
        ).scalar_one() == 9


def test_write_exhaustion_raises_database_busy(app):
    coordinator = SQLiteWriteCoordinator(
        app.state.session_factory,
        retry_delays=(0.0, 0.0),
    )
    with pytest.raises(DatabaseBusy) as captured:
        coordinator.write("persist_scan", lambda session: (_ for _ in ()).throw(locked_error()))
    assert captured.value.operation_name == "persist_scan"
    assert str(captured.value) == "数据库繁忙，扫描结果未能保存，请重试"


def test_non_transient_error_is_not_retried(app):
    coordinator = SQLiteWriteCoordinator(app.state.session_factory, (0.0, 0.0))
    attempts = 0

    def operation(session):
        nonlocal attempts
        attempts += 1
        raise ValueError("programming error")

    with pytest.raises(ValueError, match="programming error"):
        coordinator.write("broken", operation)
    assert attempts == 1


def test_write_and_write_once_share_one_reentrant_lock(app):
    coordinator = SQLiteWriteCoordinator(app.state.session_factory, (0.0,))
    entered = []
    barrier = threading.Barrier(2)

    def first():
        with coordinator.write_once("first"):
            entered.append("first")
            barrier.wait()
            coordinator.write("nested", lambda session: entered.append("nested"))

    thread = threading.Thread(target=first)
    thread.start()
    barrier.wait()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert entered == ["first", "nested"]


def test_write_once_converts_transient_error_to_database_busy(app):
    coordinator = SQLiteWriteCoordinator(app.state.session_factory, (0.0,))
    with pytest.raises(DatabaseBusy) as captured:
        with coordinator.write_once("api_update_device"):
            raise locked_error("database is busy")
    assert captured.value.operation_name == "api_update_device"
```

- [ ] **Step 3: Run tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_sqlite_writes.py -q`

Expected: collection/import failures because the setting and module do not exist.

- [ ] **Step 4: Implement retry delay parsing**

In `app/config.py`, import `field_validator` and add:

```python
sqlite_write_retry_delays: tuple[float, ...] = (0.1, 0.3, 0.8, 1.5, 3.0)

@field_validator("sqlite_write_retry_delays", mode="before")
@classmethod
def parse_sqlite_write_retry_delays(cls, value):
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("SQLite 写入重试间隔不能为空")
        try:
            value = tuple(float(part.strip()) for part in value.split(","))
        except ValueError as exc:
            raise ValueError("SQLite 写入重试间隔必须是逗号分隔的秒数") from exc
    parsed = tuple(float(delay) for delay in value)
    if not parsed or any(delay < 0 for delay in parsed):
        raise ValueError("SQLite 写入重试间隔必须至少包含一个非负秒数")
    return parsed
```

- [ ] **Step 5: Implement the coordinator**

Create `app/services/sqlite_writes.py`:

```python
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
T = TypeVar("T")
DATABASE_BUSY_MESSAGE = "数据库繁忙，扫描结果未能保存，请重试"


def is_transient_sqlite_write_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database" in message and (
        "locked" in message or "busy" in message
    )


class DatabaseBusy(RuntimeError):
    def __init__(self, operation_name: str) -> None:
        super().__init__(DATABASE_BUSY_MESSAGE)
        self.operation_name = operation_name


class SQLiteWriteCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retry_delays: tuple[float, ...],
        *,
        enabled: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.retry_delays = retry_delays
        self.enabled = enabled
        self._lock = threading.RLock()

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        if self.enabled:
            with self._lock:
                yield
        else:
            yield

    @contextmanager
    def write_once(self, operation_name: str) -> Iterator[None]:
        with self._serialized():
            try:
                yield
            except Exception as exc:
                if self.enabled and is_transient_sqlite_write_error(exc):
                    raise DatabaseBusy(operation_name) from exc
                raise

    def write(
        self,
        operation_name: str,
        operation: Callable[[Session], T],
    ) -> T:
        attempts = len(self.retry_delays) + 1
        with self._serialized():
            for attempt in range(attempts):
                with self.session_factory() as session:
                    try:
                        result = operation(session)
                        session.commit()
                        return result
                    except Exception as exc:
                        session.rollback()
                        if not self.enabled or not is_transient_sqlite_write_error(exc):
                            raise
                        if attempt == len(self.retry_delays):
                            raise DatabaseBusy(operation_name) from exc
                        logger.warning(
                            "SQLite 写入繁忙 operation=%s attempt=%s/%s",
                            operation_name,
                            attempt + 1,
                            attempts,
                        )
                time.sleep(self.retry_delays[attempt])
        raise AssertionError("SQLite 写入重试循环未返回")
```

- [ ] **Step 6: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_sqlite_writes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit coordinator foundation**

```bash
git add app/config.py app/services/sqlite_writes.py tests/test_config.py tests/test_sqlite_writes.py
git commit -m "feat: coordinate and retry sqlite writes"
```

### Task 2: Enforce one application process per SQLite file

**Files:**
- Create: `app/services/process_guard.py`
- Modify: `app/main.py`
- Test: `tests/test_process_guard.py`
- Create: `tests/test_main.py`

**Interfaces:**
- Produces: `sqlite_database_path(database_url: str) -> Path | None`.
- Produces: `SQLiteProcessAlreadyRunning(RuntimeError)`.
- Produces: `SQLiteProcessGuard(database_path: Path | None)` with `acquire()` and `release()`.
- `create_app()` stores the guard as `app.state.sqlite_process_guard` and owns it for the lifespan.

- [ ] **Step 1: Add failing path and lock behavior tests**

Create `tests/test_process_guard.py`:

```python
from pathlib import Path

import pytest

from app.services.process_guard import (
    SQLiteProcessAlreadyRunning,
    SQLiteProcessGuard,
    sqlite_database_path,
)


def test_sqlite_database_path_resolves_file_and_skips_non_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sqlite_database_path("sqlite:///./app.db") == (tmp_path / "app.db").resolve()
    assert sqlite_database_path("sqlite:///:memory:") is None
    assert sqlite_database_path("postgresql://db/app") is None


def test_second_guard_fails_until_first_releases(tmp_path):
    database_path = tmp_path / "guarded.db"
    first = SQLiteProcessGuard(database_path)
    second = SQLiteProcessGuard(database_path)
    first.acquire()
    try:
        with pytest.raises(SQLiteProcessAlreadyRunning, match="只支持一个应用进程"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_guards_for_different_databases_do_not_conflict(tmp_path):
    first = SQLiteProcessGuard(tmp_path / "first.db")
    second = SQLiteProcessGuard(tmp_path / "second.db")
    first.acquire()
    second.acquire()
    second.release()
    first.release()


def test_memory_database_guard_is_noop():
    guard = SQLiteProcessGuard(None)
    guard.acquire()
    guard.acquire()
    guard.release()
```

- [ ] **Step 2: Add failing lifespan ownership test**

Create `tests/test_main.py`:

```python
from fastapi.testclient import TestClient


def test_lifespan_acquires_and_releases_sqlite_process_guard(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.state.sqlite_process_guard, "acquire", lambda: calls.append("acquire"))
    monkeypatch.setattr(app.state.sqlite_process_guard, "release", lambda: calls.append("release"))
    with TestClient(app):
        assert calls == ["acquire"]
    assert calls == ["acquire", "release"]
```

- [ ] **Step 3: Run tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_process_guard.py tests/test_main.py -q`

Expected: FAIL because the guard module/state does not exist.

- [ ] **Step 4: Implement cross-platform process guard**

Create `app/services/process_guard.py` with this complete behavior:

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.engine import make_url

PROCESS_GUARD_MESSAGE = (
    "SQLite 模式只支持一个应用进程；请移除 uvicorn --workers，"
    "远程扫描并发由 SCAN_MAX_WORKERS 提供"
)


def sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        return None
    return Path(url.database).resolve()


class SQLiteProcessAlreadyRunning(RuntimeError):
    pass


class SQLiteProcessGuard:
    def __init__(self, database_path: Path | None) -> None:
        self.database_path = database_path
        self.lock_path = (
            database_path.with_name(f"{database_path.name}.app.lock")
            if database_path is not None
            else None
        )
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self.lock_path is None or self._handle is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise SQLiteProcessAlreadyRunning(PROCESS_GUARD_MESSAGE) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
```

- [ ] **Step 5: Wire guard into application lifespan**

In `app/main.py`, construct before `lifespan`:

```python
sqlite_process_guard = SQLiteProcessGuard(
    sqlite_database_path(resolved.database_url)
)
```

At the start of lifespan, acquire before migrations; wrap all startup/shutdown in `try/finally`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    sqlite_process_guard.acquire()
    try:
        init_database(engine)
        with session_factory() as session:
            setting = session.get(SystemSetting, 1)
            if setting is None:
                session.add(
                    SystemSetting(
                        id=1,
                        history_retention_days=resolved.history_retention_days,
                    )
                )
                session.commit()
        app.state.scan_queue.start()
        if app.state.scheduler:
            app.state.scheduler.start()
        app.state.import_test_service.resume_pending()
        try:
            yield
        finally:
            if app.state.scheduler:
                app.state.scheduler.shutdown()
            app.state.import_executor.shutdown(wait=True, cancel_futures=False)
            app.state.scan_queue.shutdown()
    finally:
        engine.dispose()
        sqlite_process_guard.release()
```

Expose it:

```python
app.state.sqlite_process_guard = sqlite_process_guard
```

- [ ] **Step 6: Run guard and existing startup tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_process_guard.py tests/test_main.py tests/test_database.py -q`

Expected: PASS.

- [ ] **Step 7: Commit process protection**

```bash
git add app/services/process_guard.py app/main.py tests/test_process_guard.py tests/test_main.py
git commit -m "feat: prevent multi-process sqlite startup"
```

### Task 3: Split collection from atomic persistence

**Files:**
- Modify: `app/services/scans.py`
- Modify: `app/services/scan_queue.py`
- Modify: `app/routes/api.py`
- Modify: `app/main.py`
- Test: `tests/test_services.py`
- Test: `tests/test_scan_queue.py`
- Test: `tests/test_scan_queue_api.py`
- Test: `tests/test_windows_optional.py`

**Interfaces:**
- Produces immutable `ScanTarget` and `ScanOutcome` dataclasses in `app.services.scans`.
- Changes `ScanService.__init__` to consume `session_factory`, cipher, and collectors.
- Produces `ScanService.collect(device_id, trigger) -> ScanOutcome` with no database writes during remote collection.
- Produces `add_scan_outcome(session: Session, outcome: ScanOutcome) -> ScanRun`; it mutates scan/device rows but never commits.
- Changes `ScanQueueService.__init__(session_factory, cipher, linux_collector, windows_collector, write_coordinator, *, max_workers, queue_size, on_successful_scan=None)`.
- Consumes `SQLiteWriteCoordinator.write("persist_scan", operation)` from Task 1.

- [ ] **Step 1: Add failing outcome and no-write-during-collection tests**

In `tests/test_services.py`, replace direct `run()` expectations with:

```python
class FailingRecordingCollector:
    def __init__(self, error):
        self.error = error
        self.seen_devices = []

    def collect(self, device, password):
        self.seen_devices.append(device)
        raise self.error

    def test_connection(self, device, password):
        self.seen_devices.append(device)
        raise self.error


def seed_service_device(
    app,
    *,
    host,
    collection_enabled=True,
    password="secret",
):
    with app.state.session_factory() as session:
        device = Device(
            name=f"device-{host}",
            host=host,
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(password),
            collection_enabled=collection_enabled,
        )
        session.add(device)
        session.commit()
        return device.id


def test_collect_returns_detached_success_outcome_without_scan_run(app):
    collector = RecordingCollector()
    device_id = seed_service_device(app, host="10.0.0.18")
    service = ScanService(
        app.state.session_factory,
        app.state.cipher,
        linux_collector=collector,
        windows_collector=collector,
    )

    outcome = service.collect(device_id, ScanTrigger.MANUAL)

    assert outcome.device_id == device_id
    assert outcome.status == ScanStatus.SUCCESS
    assert outcome.error_code is None
    assert collector.seen_devices[0].device_id == device_id
    with app.state.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ScanRun)
        ) == 0


def test_collect_converts_collector_error_without_writing(app):
    collector = FailingRecordingCollector(
        CollectorError("authentication_failed", "认证失败")
    )
    device_id = seed_service_device(app, host="10.0.0.19")
    outcome = ScanService(
        app.state.session_factory,
        app.state.cipher,
        collector,
        collector,
    ).collect(device_id, ScanTrigger.MANUAL)
    assert outcome.status == ScanStatus.FAILED
    assert outcome.error_code == "authentication_failed"
    assert outcome.error_message == "认证失败"


def test_collect_refuses_marker_before_collector(app):
    collector = RecordingCollector()
    marker_id = seed_service_device(
        app,
        host="10.0.0.20",
        collection_enabled=False,
        password="",
    )
    with pytest.raises(CollectionDisabled, match="仅用于集群标注"):
        ScanService(
            app.state.session_factory,
            app.state.cipher,
            collector,
            collector,
        ).collect(marker_id, ScanTrigger.MANUAL)
    assert collector.seen_devices == []
```

- [ ] **Step 2: Add failing atomic persistence test**

Add to `tests/test_scan_queue.py`:

```python
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
```

- [ ] **Step 3: Run focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_services.py tests/test_scan_queue.py -k "collect or atomically or marker" -q`

Expected: FAIL because `collect`, outcome dataclasses, and atomic persistence do not exist.

- [ ] **Step 4: Implement detached targets and outcomes**

In `app/services/scans.py`, define:

```python
@dataclass(frozen=True)
class ScanTarget:
    device_id: int
    os_type: OSType
    host: str
    port: int
    username: str
    encrypted_password: str


@dataclass(frozen=True)
class ScanOutcome:
    device_id: int
    trigger: ScanTrigger
    status: ScanStatus
    started_at: datetime
    finished_at: datetime
    connections: tuple[NormalizedConnection, ...] = ()
    warning_message: str | None = None
    error_code: str | None = None
    error_message: str | None = None
```

Change the constructor to store `session_factory`. Implement `_load_target()` with a short `with session_factory()` scope, then implement `collect()` so the session is closed before decrypting and calling the collector. Use `logger.exception` for non-`CollectorError` failures and return a sanitized `internal_error` outcome.

- [ ] **Step 5: Implement session-local persistence without commit**

In `app/services/scans.py`, add:

```python
def add_scan_outcome(session: Session, outcome: ScanOutcome) -> ScanRun:
    device = session.get(Device, outcome.device_id)
    if device is None:
        raise DeviceNotFound(f"设备 {outcome.device_id} 不存在")
    run = ScanRun(
        device_id=device.id,
        trigger_type=outcome.trigger,
        status=outcome.status,
        started_at=outcome.started_at,
        finished_at=outcome.finished_at,
        connection_count=len(outcome.connections),
        warning_message=outcome.warning_message,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )
    session.add(run)
    session.flush()
    session.add_all([
        ConnectionRecord(
            scan_run_id=run.id,
            protocol=row.protocol,
            address_family=row.address_family,
            local_ip=row.local_ip,
            local_port=row.local_port,
            remote_ip=row.remote_ip,
            remote_port=row.remote_port,
            state=row.state,
            pid=row.pid,
            process_name=row.process_name,
        )
        for row in outcome.connections
    ])
    device.last_scan_status = outcome.status
    device.last_scan_at = outcome.finished_at
    return run
```

- [ ] **Step 6: Make queue persistence atomic**

Inject `write_coordinator` into `ScanQueueService`. Replace `_execute_task` with a read/collect/write flow:

```python
with self.session_factory() as session:
    task = session.get(ScanTask, task_id)
    if task is None or task.status != ScanTaskStatus.RUNNING:
        return
    device_id = task.device_id
    trigger = task.trigger_type

outcome = self.scan_service.collect(device_id, trigger)

def persist(session):
    task = session.get(ScanTask, task_id)
    if task is None or task.status != ScanTaskStatus.RUNNING:
        return False
    run = add_scan_outcome(session, outcome)
    task.scan_run_id = run.id
    task.finished_at = outcome.finished_at
    task.error_message = outcome.error_message
    task.status = (
        ScanTaskStatus.SUCCESS
        if outcome.status == ScanStatus.SUCCESS
        else ScanTaskStatus.FAILED
    )
    batch_ids = set()
    for item in task.items:
        item.status = task.status
        batch_ids.add(item.batch_id)
    session.flush()
    for batch_id in batch_ids:
        self._refresh_batch(session, batch_id)
    return task.status == ScanTaskStatus.SUCCESS

successful = self.write_coordinator.write("persist_scan", persist)
```

Build one `ScanService` in the queue constructor from `session_factory`. Remove the old `ScanService.run()` commit logic. Update `_scan_service` in `app/routes/api.py` to pass `request.app.state.session_factory`.

In `app/main.py`, construct the coordinator immediately after `session_factory`, then expose it with the other `app.state` assignments after the `FastAPI` object is created:

```python
sqlite_write_coordinator = SQLiteWriteCoordinator(
    session_factory,
    resolved.sqlite_write_retry_delays,
    enabled=engine.dialect.name == "sqlite",
)

# after app = FastAPI(...)
app.state.sqlite_write_coordinator = sqlite_write_coordinator
```

Pass it to the new `ScanQueueService` constructor after the two collectors.

- [ ] **Step 7: Adapt existing service/platform tests**

Update every `ScanService(session, ...)` construction in `tests/test_services.py` and `tests/test_windows_optional.py` to use `app.state.session_factory`; assert returned `ScanOutcome` fields instead of a committed run. Preserve the Windows unavailable error code assertion.

- [ ] **Step 8: Run scan and API suites**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_services.py tests/test_scan_queue.py tests/test_scan_queue_api.py tests/test_api.py tests/test_windows_optional.py -q`

Expected: PASS.

- [ ] **Step 9: Commit atomic scan persistence**

```bash
git add app/services/scans.py app/services/scan_queue.py app/routes/api.py app/main.py tests/test_services.py tests/test_scan_queue.py tests/test_scan_queue_api.py tests/test_windows_optional.py
git commit -m "feat: persist scan outcomes atomically"
```

### Task 4: Coordinate every scan queue mutation and classify retry exhaustion

**Files:**
- Modify: `app/services/scan_queue.py`
- Test: `tests/test_scan_queue.py`
- Test: `tests/test_scan_queue_api.py`

**Interfaces:**
- All queue mutation methods consume `SQLiteWriteCoordinator.write(...)` and no longer own independent commit blocks.
- Produces `_record_database_busy(task_id: int, outcome: ScanOutcome) -> None`.
- A persistence retry exhaustion creates one failed `ScanRun(error_code="database_busy")`, links it to the failed task, and updates batch counts in one later coordinated transaction.

- [ ] **Step 1: Add failing lock-retry and database-busy tests**

Add to `tests/test_scan_queue.py`:

```python
def test_claims_enqueue_and_batch_mutations_use_shared_coordinator(app, monkeypatch):
    calls = []
    original = app.state.sqlite_write_coordinator.write

    def recording_write(name, operation):
        calls.append(name)
        return original(name, operation)

    monkeypatch.setattr(app.state.sqlite_write_coordinator, "write", recording_write)
    device_id = seed_devices(app, 1)[0]
    queue = app.state.scan_queue
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    queue.cancel_device(device_id)
    assert {"create_scan_batch", "claim_scan_tasks", "cancel_scan_device"} <= set(calls)
    assert batch.id
    assert task_id


def test_persist_retry_exhaustion_records_database_busy(app, monkeypatch):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app)
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    original = queue.write_coordinator.write

    def fail_persist(name, operation):
        if name == "persist_scan":
            raise DatabaseBusy(name)
        return original(name, operation)

    monkeypatch.setattr(queue.write_coordinator, "write", fail_persist)
    queue._execute_safely(task_id)

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        run = session.get(ScanRun, task.scan_run_id)
        persisted_batch = session.get(ScanBatch, batch.id)
        assert task.status == ScanTaskStatus.FAILED
        assert task.error_message == DATABASE_BUSY_MESSAGE
        assert run.error_code == "database_busy"
        assert run.error_message == DATABASE_BUSY_MESSAGE
        assert session.scalar(
            select(func.count()).select_from(ConnectionRecord).where(
                ConnectionRecord.scan_run_id == run.id
            )
        ) == 0
        assert persisted_batch.failed_tasks == 1
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scan_queue.py -k "shared_coordinator or database_busy" -q`

Expected: FAIL because queue mutations still commit directly and exhaustion is generic.

- [ ] **Step 3: Refactor queue mutation methods to replayable closures**

Convert these methods so their closures receive the coordinator-created session and never call `commit()`:

```text
enqueue_device                 operation name enqueue_scan_device
create_batch                   operation name create_scan_batch
create_import_scan_batch       operation name create_import_scan_batch
recover_running_tasks          operation name recover_scan_tasks
_claim_tasks                   operation name claim_scan_tasks
_record_database_busy          operation name record_database_busy
_fail_unexpected_task          operation name fail_scan_task
cancel_device                  operation name cancel_scan_device
```

Each public method returns scalar IDs or dataclass/read-model-safe values, then reloads ORM objects with a short read session only where existing callers require an ORM response. Do not return an ORM instance created inside a failed/replayed attempt.

- [ ] **Step 4: Implement explicit database-busy persistence**

Catch `DatabaseBusy` around `write_coordinator.write("persist_scan", persist)` inside `_execute_task`, where the detached `outcome` is still available. `_record_database_busy` must create this failure outcome and persist it with the same task/batch mutation logic:

```python
busy_outcome = ScanOutcome(
    device_id=device_id,
    trigger=trigger,
    status=ScanStatus.FAILED,
    started_at=outcome.started_at,
    finished_at=datetime.now(timezone.utc),
    error_code="database_busy",
    error_message=DATABASE_BUSY_MESSAGE,
)
```

Call coordinator operation `record_database_busy`. If that second operation also exhausts retries, log `logger.exception("扫描任务 %s 数据库繁忙状态无法保存", task_id)` and leave the task recoverable as `RUNNING`; do not propagate into `_execute_safely` and do not relabel it `internal_error`.

- [ ] **Step 5: Run all queue tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scan_queue.py tests/test_scan_queue_api.py -q`

Expected: PASS.

- [ ] **Step 6: Commit coordinated queue mutations**

```bash
git add app/services/scan_queue.py tests/test_scan_queue.py tests/test_scan_queue_api.py
git commit -m "feat: coordinate scan queue writes"
```

### Task 5: Share coordination with imports, scheduler, and API writes

**Files:**
- Modify: `app/services/import_testing.py`
- Modify: `app/services/imports.py`
- Modify: `app/services/scheduler.py`
- Modify: `app/routes/api.py`
- Modify: `app/main.py`
- Test: `tests/test_import_testing.py`
- Test: `tests/test_imports.py`
- Test: `tests/test_scheduler_queue.py`
- Test: `tests/test_api.py`

**Interfaces:**
- `ImportTestService` consumes the application coordinator and removes its private `_database_gate`, `_run_database_operation`, duplicate transient detector, and retry constants.
- `import_devices(..., write_coordinator)` enters `write_once` before every transaction that can flush.
- `ImportTestService.__init__(session_factory, cipher, executor, linux_collector, windows_collector, write_coordinator, batch_completed_callback=None)`.
- `SchedulerService.__init__(session_factory, scan_queue, scan_jitter_seconds, write_coordinator, on_history_purged=None)` consumes the coordinator for history purge writes.
- Mutating API routes use `write_once` before ORM mutation/flush and map `DatabaseBusy` to HTTP 503.

- [ ] **Step 1: Add failing shared-coordinator coverage**

Add focused assertions:

```python
# tests/test_import_testing.py
def test_import_testing_uses_application_write_coordinator(app, monkeypatch):
    names = []
    original = app.state.sqlite_write_coordinator.write
    monkeypatch.setattr(
        app.state.sqlite_write_coordinator,
        "write",
        lambda name, operation: (names.append(name), original(name, operation))[1],
    )
    batch_id, row_id, _ = seed_pending_row(app, "10.0.0.88")
    app.state.import_test_service.test_row(row_id)
    assert "claim_import_test_row" in names
    assert "save_import_test_result" in names


# tests/test_imports.py
def test_import_transactions_enter_shared_write_once(app, monkeypatch):
    names = []
    original = app.state.sqlite_write_coordinator.write_once
    @contextmanager
    def recording(name):
        names.append(name)
        with original(name):
            yield
    monkeypatch.setattr(app.state.sqlite_write_coordinator, "write_once", recording)
    with app.state.session_factory() as session:
        import_devices(
            session,
            app.state.cipher,
            "devices.xlsx",
            workbook_bytes([("one", "10.0.0.89", "linux", 22, "ops", "secret", "", 5, "否")]),
            app.state.sqlite_write_coordinator,
        )
    assert names == ["create_import_batch", "import_device_row", "finish_import_batch"]


# tests/test_scheduler_queue.py
def test_history_purge_enters_shared_write_once(app, monkeypatch):
    names = []
    original = app.state.sqlite_write_coordinator.write_once

    @contextmanager
    def recording(name):
        names.append(name)
        with original(name):
            yield

    monkeypatch.setattr(app.state.sqlite_write_coordinator, "write_once", recording)
    scheduler = SchedulerService(
        app.state.session_factory,
        RecordingQueue(),
        0,
        app.state.sqlite_write_coordinator,
    )
    scheduler._purge_history()
    assert names == ["purge_history"]


# tests/test_api.py
def test_mutating_routes_enter_shared_write_once(
    client,
    app,
    linux_device_payload,
    monkeypatch,
):
    names = []
    original = app.state.sqlite_write_coordinator.write_once

    @contextmanager
    def recording(name):
        names.append(name)
        with original(name):
            yield

    monkeypatch.setattr(app.state.sqlite_write_coordinator, "write_once", recording)
    assert client.post("/api/clusters", json={"name": "coordinated"}).status_code == 201
    assert client.post("/api/devices", json=linux_device_payload).status_code == 201
    assert client.put(
        "/api/settings",
        json={"history_retention_days": 11},
    ).status_code == 200
    assert {
        "api_create_cluster",
        "api_create_device",
        "api_update_settings",
    } <= set(names)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py tests/test_imports.py tests/test_scheduler_queue.py tests/test_api.py -k "coordinator or write_once" -q`

Expected: FAIL because these services/routes do not consume the shared coordinator.

- [ ] **Step 3: Replace import test private coordination**

Add `write_coordinator: SQLiteWriteCoordinator` to `ImportTestService.__init__`. Refactor mutation methods to coordinator closures with exact names:

```text
resume_pending recovery       recover_import_tests
_load_target                  claim_import_test_row
_save_result                  save_import_test_result
```

Read-only row ID loads remain ordinary short sessions. Remove `DATABASE_RETRY_DELAYS`, `_is_transient_database_error`, `_database_gate`, and `_run_database_operation` from `app/services/import_testing.py`.

- [ ] **Step 4: Coordinate import row transactions**

Change signature:

```python
def import_devices(
    session: Session,
    cipher: CredentialCipher,
    filename: str,
    content: bytes,
    write_coordinator: SQLiteWriteCoordinator,
) -> ImportBatch:
```

Wrap the three transaction boundaries before any mutation/flush:

```python
with write_coordinator.write_once("create_import_batch"):
    session.add(batch)
    session.commit()

for row_number, values in rows:
    with write_coordinator.write_once("import_device_row"):
        import_one_row(session, cipher, batch.id, row_number, values)

with write_coordinator.write_once("finish_import_batch"):
    finish_import_batch(session, batch.id)
```

Implement `import_one_row(...)` by moving the current `for row_number, values in rows` loop's complete `try/except` body into the helper, including duplicate handling, cluster creation, marker-device handling, row-level rollback, counter updates, and its final commit. Implement `finish_import_batch(...)` by moving the current final batch reload, `TESTING`/`COMPLETED` selection, `finished_at`, commit, refresh, and return logic into that helper. The mechanical extraction must not change any import messages, counters, validation, or logging.

Update the upload route and all test callers to pass `app.state.sqlite_write_coordinator`.

- [ ] **Step 5: Coordinate scheduler writes**

Inject coordinator into `SchedulerService`. In `_purge_history`, wrap the entire `purge_expired_scans(session, days)` call and its internal explicit delete/commit in `write_once("purge_history")`. Scheduler enqueue already delegates to the coordinated queue.

- [ ] **Step 6: Coordinate mutating API transactions and map busy errors**

Add helper in `app/routes/api.py`:

```python
@contextmanager
def _write_request(request: Request, operation_name: str):
    try:
        with request.app.state.sqlite_write_coordinator.write_once(operation_name):
            yield
    except DatabaseBusy as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
```

Enter this helper before any ORM mutation, helper that may flush, explicit `flush`, or `commit` in these routes:

```text
add_cluster       api_create_cluster
edit_cluster      api_update_cluster
remove_cluster    api_delete_cluster
create_device     api_create_device (after remote connection test)
update_device     api_update_device (after remote connection test)
delete_device     api_delete_device
update_settings   api_update_settings
upload_import     handled inside import_devices transaction boundaries
scan batch routes handled inside ScanQueueService
```

Keep scheduler synchronization and topology cache clearing after successful commits and outside the write lock.

- [ ] **Step 7: Pass the existing application coordinator everywhere**

Pass `app.state.sqlite_write_coordinator` to the import test service and scheduler constructors. The queue already received it in Task 3. Wrap the initial `SystemSetting` lookup, possible insertion, and commit in `write_once("initialize_settings")` before any `session.add()`.

- [ ] **Step 8: Run import, scheduler, route, and cluster suites**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py tests/test_imports.py tests/test_scheduler_queue.py tests/test_api.py tests/test_clusters.py tests/test_retention.py -q`

Expected: PASS.

- [ ] **Step 9: Commit shared coordination coverage**

```bash
git add app/services/import_testing.py app/services/imports.py app/services/scheduler.py app/routes/api.py app/main.py tests/test_import_testing.py tests/test_imports.py tests/test_scheduler_queue.py tests/test_api.py
git commit -m "feat: share sqlite write coordination"
```

### Task 6: Deterministic concurrency regressions and operational documentation

**Files:**
- Create: `tests/test_scan_write_concurrency.py`
- Modify: `README.md`
- Modify: `.env.example` if present
- Test: `tests/test_scan_write_concurrency.py`

**Interfaces:**
- Verifies the combined Tasks 1–5 contract under 30 simultaneous remote completions.
- Documents the exact startup and retry configuration supported in SQLite mode.

- [ ] **Step 1: Add a deterministic 30-device concurrency test**

Create `tests/test_scan_write_concurrency.py`:

```python
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
        app.state.sqlite_write_coordinator,
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
```

Use the existing test application database; the repeated five-run check in Step 4 makes lock regressions deterministic without adding a second app factory.

- [ ] **Step 2: Add deterministic retry-without-duplicates test**

Append to the same file:

```python
def transient_locked_error():
    return OperationalError("INSERT INTO scan_runs", (), Exception("database is locked"))


def test_persist_retry_rebuilds_rows_without_duplicates(app, monkeypatch):
    device_id = seed_devices(app, 1)[0]
    queue = make_queue(app, BarrierCollector(1), 1)
    batch = queue.create_batch(ScanBatchType.ALL, [device_id])
    task_id = queue._claim_next_task()
    original_write = app.state.sqlite_write_coordinator.write
    persist_attempts = 0

    def flaky_write(name, operation):
        if name != "persist_scan":
            return original_write(name, operation)

        def flaky_operation(session):
            nonlocal persist_attempts
            persist_attempts += 1
            result = operation(session)
            if persist_attempts < 3:
                raise transient_locked_error()
            return result

        return original_write(name, flaky_operation)

    monkeypatch.setattr(
        app.state.sqlite_write_coordinator,
        "write",
        flaky_write,
    )
    queue._execute_task(task_id)

    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        assert persist_attempts == 3
        assert task.status == ScanTaskStatus.SUCCESS
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 1
        assert session.scalar(select(func.count()).select_from(ConnectionRecord)) == 1
        assert session.get(ScanBatch, batch.id).success_tasks == 1
```

- [ ] **Step 3: Add mixed import-test and scan-write test**

Append:

```python
def test_import_test_and_scan_finish_together_without_internal_errors(app):
    collector = BarrierCollector(2)
    device_ids = seed_devices(app, 2)
    queue = make_queue(app, collector, 1)
    scan_batch = queue.create_batch(ScanBatchType.ALL, [device_ids[0]])

    with app.state.sqlite_write_coordinator.write_once("seed_import_test"):
        with app.state.session_factory() as session:
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
        app.state.sqlite_write_coordinator,
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
```

- [ ] **Step 4: Run concurrency tests repeatedly**

Run:

```powershell
1..5 | ForEach-Object {
  .\.venv\Scripts\python.exe -m pytest tests/test_scan_write_concurrency.py -q
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Expected: all five runs PASS.

- [ ] **Step 5: Update operations documentation**

In `README.md`, replace the existing single-process warning with an explanation that:

```text
- SQLite 模式必须只运行一个 Uvicorn 进程；应用会在启动时强制检查。
- 不要使用 uvicorn --workers。
- SCAN_MAX_WORKERS=30 已提供 30 路 SSH/WinRM 并行采集。
- SQLite 写入会短暂排队，不会降低远程连接并发。
- SQLITE_WRITE_RETRY_DELAYS=0.1,0.3,0.8,1.5,3 控制锁冲突退避。
```

Add the new setting to `.env.example` only if that tracked file exists.

- [ ] **Step 6: Run complete verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -c "from app.config import Settings; s=Settings(_env_file=None); assert s.scan_max_workers == 30; assert s.sqlite_write_retry_delays == (0.1,0.3,0.8,1.5,3.0); print('sqlite concurrency contracts ok')"
git diff --check
```

Expected: Ruff PASS, pytest zero failures, `sqlite concurrency contracts ok`, and no whitespace errors.

- [ ] **Step 7: Search logs and error mappings for sensitive or misleading output**

Run:

```powershell
rg -n "database is locked|database is busy|database_busy|internal_error|SQLite 写入繁忙|password|encrypted_password" app tests
```

Expected: transient DB strings exist only in detection/tests; retry logs contain no credential values or SQL parameters; `database_busy` has its exact user-facing message; genuine collector exceptions remain separate.

- [ ] **Step 8: Commit concurrency proof and documentation**

```bash
git add tests/test_scan_write_concurrency.py README.md .env.example
git commit -m "test: verify concurrent sqlite scan persistence"
```

If `.env.example` does not exist, omit it from `git add`.
