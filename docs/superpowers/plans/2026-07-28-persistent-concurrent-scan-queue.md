# Persistent Concurrent Scan Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace direct per-device scanning with a persistent, deduplicating, configurable thread-based queue that can process hundreds to 1000 devices and expose batch progress.

**Architecture:** Web requests, Excel import completion, and APScheduler enqueue persistent database tasks. One application process runs a fixed worker-thread pool that claims tasks by priority, executes scans with independent SQLAlchemy sessions, and settles all linked batches.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy 2, SQLite WAL, APScheduler, Paramiko, optional pywinrm, Jinja2, vanilla JavaScript, pytest

## Global Constraints

- Run exactly one Uvicorn application process; do not use `--workers`.
- Default full-scan concurrency is 30 threads.
- Default import connection-test concurrency is 20 threads.
- Default active queue capacity is 2000 unique device tasks.
- Default scheduling jitter is 300 seconds.
- Default SQLite busy timeout is 30000 milliseconds.
- A device may have at most one `pending` or `running` task.
- Repeated requests reuse the active task and may raise its priority.
- Queue state and batch progress survive application restart.
- Missing `pywinrm` must not affect startup or Linux scanning.
- Do not perform Git operations.

---

### Task 1: Add configuration, SQLite concurrency settings, and queue models

**Files:**
- Modify: `app/config.py`
- Modify: `app/database.py`
- Modify: `app/models.py`
- Modify: `app/migrations.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`
- Test: `tests/test_migrations.py`
- Create: `tests/test_database.py`

**Interfaces:**
- Produces: `Settings.import_test_max_workers: int`
- Produces: `Settings.scan_max_workers: int`
- Produces: `Settings.scan_queue_size: int`
- Produces: `Settings.scan_jitter_seconds: int`
- Produces: `Settings.sqlite_busy_timeout_ms: int`
- Produces: `ScanBatch`, `ScanTask`, and `ScanBatchItem` ORM models and enums
- Produces: `create_database_engine(database_url: str, sqlite_busy_timeout_ms: int = 30000) -> Engine`

- [ ] **Step 1: Write failing configuration and migration tests**

Add configuration assertions:

```python
def test_scan_concurrency_defaults(valid_key):
    settings = Settings(app_secret_key=valid_key, _env_file=None)
    assert settings.import_test_max_workers == 20
    assert settings.scan_max_workers == 30
    assert settings.scan_queue_size == 2000
    assert settings.scan_jitter_seconds == 300
    assert settings.sqlite_busy_timeout_ms == 30000
```

Extend migration tests to assert the new tables, partial unique index, and
`import_batches.scan_batch_id` exist while existing rows remain intact.

Add a SQLite connection test that queries:

```sql
PRAGMA journal_mode
PRAGMA foreign_keys
PRAGMA busy_timeout
```

Expected values for a file database are `wal`, `1`, and the configured timeout.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_database.py tests\test_migrations.py -q
```

Expected: failures for missing settings, models, tables, and PRAGMA setup.

- [ ] **Step 3: Add validated settings**

Add:

```python
import_test_max_workers: int = Field(default=20, ge=1, le=200)
scan_max_workers: int = Field(default=30, ge=1, le=200)
scan_queue_size: int = Field(default=2000, ge=1, le=100000)
scan_jitter_seconds: int = Field(default=300, ge=0, le=86400)
sqlite_busy_timeout_ms: int = Field(default=30000, ge=1000, le=300000)
```

Add the same names and values to `.env.example`.

- [ ] **Step 4: Configure SQLite connections**

Update `create_database_engine()` to pass a timeout and register a SQLAlchemy
`connect` event:

```python
@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={sqlite_busy_timeout_ms}")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
```

Apply this only to SQLite URLs and preserve the existing non-SQLite behavior.

- [ ] **Step 5: Add queue enums and ORM models**

Define:

```python
class ScanTaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanBatchStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class ScanBatchType(str, enum.Enum):
    ALL = "all"
    CLUSTER = "cluster"
    IMPORT = "import"
```

Extend `ScanTrigger` with `IMPORT` and `BATCH`.

Create these model shapes:

```python
class ScanBatch(Base):
    id: Mapped[int]
    batch_type: Mapped[ScanBatchType]
    status: Mapped[ScanBatchStatus]
    cluster_id: Mapped[int | None]
    source_import_batch_id: Mapped[int | None]
    total_tasks: Mapped[int]
    pending_tasks: Mapped[int]
    running_tasks: Mapped[int]
    success_tasks: Mapped[int]
    failed_tasks: Mapped[int]
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class ScanTask(Base):
    id: Mapped[int]
    device_id: Mapped[int]
    trigger_type: Mapped[ScanTrigger]
    priority: Mapped[int]
    status: Mapped[ScanTaskStatus]
    scan_run_id: Mapped[int | None]
    error_message: Mapped[str | None]
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class ScanBatchItem(Base):
    id: Mapped[int]
    batch_id: Mapped[int]
    task_id: Mapped[int]
    device_id: Mapped[int]
    status: Mapped[ScanTaskStatus]
```

Use foreign keys with appropriate cascade behavior, ORM relationships, and:

```python
Index(
    "uq_scan_tasks_device_active",
    "device_id",
    unique=True,
    sqlite_where=text("status IN ('PENDING', 'RUNNING')"),
)
```

Add a unique constraint on `(batch_id, device_id)` for batch items and a unique
nullable `ScanBatch.source_import_batch_id`.

- [ ] **Step 6: Add schema version 3 migration**

Set `LATEST_SCHEMA_VERSION = 3`. For an existing database, add
`import_batches.scan_batch_id`, create its index, create the active-task partial
index, and record schema version 3. Keep every operation idempotent.

- [ ] **Step 7: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_database.py tests\test_migrations.py -q
```

Expected: all focused tests pass.

### Task 2: Implement the persistent scan queue service

**Files:**
- Create: `app/services/scan_queue.py`
- Create: `tests/test_scan_queue.py`
- Modify: `app/services/scans.py`

**Interfaces:**
- Produces: `ScanQueueFull(RuntimeError)`
- Produces: `ScanQueueService(session_factory, cipher, linux_collector, windows_collector, *, max_workers: int, queue_size: int)`
- Produces: `ScanQueueService.enqueue_device(device_id: int, trigger: ScanTrigger, priority: int, batch_id: int | None = None) -> ScanTask`
- Produces: `ScanQueueService.create_batch(batch_type: ScanBatchType, device_ids: Sequence[int], *, cluster_id: int | None = None, source_import_batch_id: int | None = None) -> ScanBatch`
- Produces: `ScanQueueService.create_import_scan_batch(import_batch_id: int) -> ScanBatch | None`
- Produces: `ScanQueueService.start() -> None`
- Produces: `ScanQueueService.shutdown() -> None`
- Produces: `ScanQueueService.cancel_device(device_id: int) -> None`

- [ ] **Step 1: Write failing queue tests**

Cover:

```python
def test_duplicate_device_reuses_task_and_raises_priority(...):
    first = queue.enqueue_device(device.id, ScanTrigger.SCHEDULED, 20)
    second = queue.enqueue_device(device.id, ScanTrigger.MANUAL, 100)
    assert second.id == first.id
    assert second.priority == 100
    assert second.trigger_type == ScanTrigger.MANUAL
```

Also test:

- Capacity counts unique active tasks and raises `ScanQueueFull`.
- One task can be linked to two different batches.
- A batch containing an already-active device still reaches completion.
- Import batch creation is idempotent.
- Thirty simultaneous claim calls return distinct task IDs.
- Recovery changes every `running` task to `pending`.
- Cancelling a device settles or removes its batch associations consistently.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_queue.py -q
```

Expected: import failure because `app.services.scan_queue` does not exist.

- [ ] **Step 3: Implement transactional enqueue and batch creation**

Use a process-local enqueue lock. Within one short transaction:

1. Query the device and active task.
2. Reuse and reprioritize the active task when present.
3. Count active tasks before creating a new task.
4. Raise `ScanQueueFull("扫描队列已满，请稍后重试")` when capacity is reached.
5. Attach an idempotent `ScanBatchItem` when a batch is supplied.
6. Recalculate batch counters.

Bulk batch creation must preflight the number of new unique tasks before writing
anything so a capacity failure does not create a partial batch.

- [ ] **Step 4: Implement worker lifecycle**

`start()` must:

```python
self._recover_running_tasks()
self._executor = ThreadPoolExecutor(
    max_workers=self.max_workers,
    thread_name_prefix="device-scan",
)
for _ in range(self.max_workers):
    self._executor.submit(self._worker_loop)
```

Each worker waits on an event, calls `_claim_next_task()` under a claim lock, and
processes tasks until shutdown. Claims order by priority descending and creation
time ascending.

`shutdown()` stops new claims, wakes idle workers, and waits for current network
operations to finish.

- [ ] **Step 5: Execute scans and settle all linked batches**

For each claimed task:

```python
run = ScanService(
    session,
    cipher,
    linux_collector,
    windows_collector,
).run(task.device_id, task.trigger_type)
```

Map `ScanRun.status` to task success or failure, store `scan_run_id`, timestamps,
and safe error text, then update every linked `ScanBatchItem`. Recalculate each
affected batch using item statuses and mark it completed when no item remains
pending or running.

- [ ] **Step 6: Keep network calls outside queue write transactions**

Ensure task claiming commits before `ScanService.run()` starts. Add a regression
test that a second SQLAlchemy session can write a separate row while a fake
collector is blocked on network I/O.

- [ ] **Step 7: Run focused queue tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_queue.py -q
```

Expected: all queue tests pass without duplicate task execution.

### Task 3: Route scheduling and application lifecycle through the queue

**Files:**
- Modify: `app/main.py`
- Modify: `app/services/scheduler.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_scheduler_queue.py`

**Interfaces:**
- Consumes: `ScanQueueService.enqueue_device(...)`
- Produces: APScheduler jobs that enqueue at priority 20 with configured jitter
- Produces: `app.state.scan_queue`

- [ ] **Step 1: Write failing scheduler tests**

Create a recording queue fake and assert:

```python
scheduler._enqueue_device(device.id)
assert queue.calls == [(device.id, ScanTrigger.SCHEDULED, 20)]
```

Inspect the scheduled job and assert `job.trigger.jitter` equals the configured
value. Assert scheduler code no longer calls `ScanService.run()` directly.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scheduler_queue.py -q
```

Expected: current scheduler directly scans and has no queue dependency.

- [ ] **Step 3: Refactor SchedulerService**

Change its constructor to consume `ScanQueueService` and
`scan_jitter_seconds`. Replace `_scan_device()` with `_enqueue_device()`:

```python
try:
    self.scan_queue.enqueue_device(device_id, ScanTrigger.SCHEDULED, 20)
except ScanQueueFull:
    logger.warning("扫描队列已满，跳过设备 %s 的本次定时任务", device_id)
```

Pass `jitter=self.scan_jitter_seconds` to interval jobs.

- [ ] **Step 4: Wire lifecycle order**

In `create_app()`:

1. Pass `sqlite_busy_timeout_ms` into `create_database_engine()`.
2. Build the import executor with `import_test_max_workers`.
3. Construct `ScanQueueService` with all collectors and queue settings.
4. Construct `SchedulerService` with the queue.

During startup:

```python
app.state.scan_queue.start()
app.state.scheduler.start()
app.state.import_test_service.resume_pending()
```

During shutdown:

```python
app.state.scheduler.shutdown()
app.state.import_executor.shutdown(wait=True, cancel_futures=False)
app.state.scan_queue.shutdown()
```

- [ ] **Step 5: Update test fixtures and run focused tests**

Use a stopped or deterministic queue in ordinary API tests so background workers
do not race test assertions.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scheduler_queue.py tests\test_pages.py -q
```

Expected: all focused tests pass.

### Task 4: Trigger first scans after Excel connection tests

**Files:**
- Modify: `app/models.py`
- Modify: `app/services/import_testing.py`
- Modify: `app/schemas.py`
- Modify: `app/main.py`
- Modify: `tests/test_import_testing.py`

**Interfaces:**
- Consumes: `ScanQueueService.create_import_scan_batch(import_batch_id: int)`
- Produces: `ImportBatchRead.scan_batch_id: int | None`

- [ ] **Step 1: Write failing import integration tests**

For two successful rows and one failed row, assert:

```python
assert batch.scan_batch_id is not None
scan_batch = session.get(ScanBatch, batch.scan_batch_id)
assert scan_batch.total_tasks == 2
```

Assert the failed connection-test device is preserved but has no batch item.
Call finalization twice and assert the same scan batch is reused.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_import_testing.py -q
```

Expected: failures because completed imports do not enqueue full scans.

- [ ] **Step 3: Add completion callback**

Give `ImportTestService` a
`batch_completed_callback: Callable[[int], None] | None`. Make
`_refresh_batch_counts()` return whether the import transitioned to completed.
After committing the final test result, invoke the callback outside that session.

The callback is:

```python
app.state.scan_queue.create_import_scan_batch
```

It must be idempotent through the unique source-import constraint.

- [ ] **Step 4: Expose the linked scan batch**

Add `scan_batch_id` to `ImportBatchRead` and ensure import polling responses
include it.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_import_testing.py tests\test_imports.py -q
```

Expected: all import tests pass.

### Task 5: Add asynchronous task and batch APIs

**Files:**
- Modify: `app/schemas.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_api.py`
- Create: `tests/test_scan_queue_api.py`

**Interfaces:**
- Produces: `ScanTaskRead`
- Produces: `ScanBatchRead`
- Produces: `BatchScanCreate`
- Produces: `POST /api/devices/{device_id}/scan`
- Produces: `GET /api/scan-tasks/{task_id}`
- Produces: `POST /api/scan-batches`
- Produces: `GET /api/scan-batches`
- Produces: `GET /api/scan-batches/{batch_id}`

- [ ] **Step 1: Write failing API tests**

Assert single-device scanning returns HTTP 202:

```python
response = client.post(f"/api/devices/{device_id}/scan")
assert response.status_code == 202
assert response.json()["status"] in {"pending", "running"}
```

Test all-device and cluster batches, recent batch listing, status polling, unknown
IDs, deduplication, and HTTP 429 on queue capacity exhaustion.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_queue_api.py tests\test_api.py -q
```

Expected: existing single-device endpoint returns a completed `ScanRead`; batch
routes do not exist.

- [ ] **Step 3: Add schemas**

Define:

```python
class BatchScanCreate(BaseModel):
    scope: Literal["all", "cluster"]
    cluster_id: int | None = Field(default=None, ge=1)


class ScanTaskRead(BaseModel):
    id: int
    device_id: int
    trigger_type: ScanTrigger
    priority: int
    status: ScanTaskStatus
    scan_run_id: int | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ScanBatchRead(BaseModel):
    id: int
    batch_type: ScanBatchType
    status: ScanBatchStatus
    total_tasks: int
    pending_tasks: int
    running_tasks: int
    success_tasks: int
    failed_tasks: int
    created_at: datetime
    finished_at: datetime | None
```

Validate that `cluster_id` is present only for cluster scope.

- [ ] **Step 4: Implement API routes**

- Single scan: enqueue with priority 100 and return HTTP 202.
- All/cluster batch: query selected device IDs, enqueue at priority 80, and
  return HTTP 201.
- Task and batch GET routes return persisted status.
- Recent batch listing orders by creation time descending and limits to 20.
- Convert `ScanQueueFull` to HTTP 429 with `"扫描队列已满，请稍后重试"`.

Before deleting a device, call `scan_queue.cancel_device(device_id)`.

- [ ] **Step 5: Run API tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_queue_api.py tests\test_api.py -q
```

Expected: all API tests pass.

### Task 6: Add batch controls and asynchronous progress to the Chinese UI

**Files:**
- Modify: `app/templates/devices.html`
- Modify: `app/templates/topology.html`
- Modify: `app/static/js/topology.js`
- Modify: `app/static/css/app.css`
- Modify: `tests/test_pages.py`

**Interfaces:**
- Consumes: task and batch APIs from Task 5
- Produces: Chinese scan-all, scan-cluster, task status, and batch progress UI

- [ ] **Step 1: Add failing page-content tests**

Assert the devices page contains:

```text
批量扫描
扫描全部设备
扫描所选集群
等待
执行中
成功
失败
```

Assert the topology page script polls `/api/scan-tasks/` instead of expecting a
synchronous scan result.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_pages.py -q
```

Expected: new batch controls are absent.

- [ ] **Step 3: Add the batch scan panel**

Add a device-page panel with:

- “扫描全部设备” button.
- Cluster selector and “扫描所选集群” button.
- Recent batch cards with total, waiting, running, success, failure, and a
  progress bar.
- Polling every 1.5 seconds while a recent batch is incomplete.
- Queue-full and empty-cluster messages in Chinese.

- [ ] **Step 4: Convert single-device buttons to async polling**

After HTTP 202, render “等待扫描”; poll `/api/scan-tasks/{id}`. Render
“扫描中” when running. On success or failure, show the result and reload only the
affected view.

Apply the same behavior to the topology page “立即采集” button and load the new
topology only after task success.

- [ ] **Step 5: Link Excel import progress**

When `scan_batch_id` appears in import polling, fetch and render its first-scan
progress below the connection-test results.

- [ ] **Step 6: Style responsive progress controls**

Add compact status cards and progress bars consistent with the current dark
interface. Ensure `[hidden]` remains authoritative and controls collapse to one
column on narrow screens.

- [ ] **Step 7: Run page and JavaScript checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_pages.py -q
node --check app\static\js\topology.js
```

Expected: tests and syntax check pass.

### Task 7: Complete regression, load, migration, and browser verification

**Files:**
- Modify: `README.md`
- Verify: all changed application and test files

**Interfaces:**
- Produces: documented Linux single-process deployment and concurrency tuning
- Produces: verified schema version 3 migration and running local service

- [ ] **Step 1: Add operational documentation**

Document the five `.env` settings, expected throughput, queue persistence,
single-Uvicorn-process requirement, and why `--workers` must not be used.

- [ ] **Step 2: Run complete static and automated checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
node --check app\static\js\topology.js
```

Expected: all checks pass.

- [ ] **Step 3: Run a bounded load test**

Use fake collectors and 1000 seeded devices. Enqueue an all-device batch with 30
workers and assert:

- Every device is executed exactly once.
- Maximum observed concurrency is no greater than 30 and greater than 1.
- Batch counters end at 1000 completed items.
- No SQLite `database is locked` error occurs.

- [ ] **Step 4: Validate the existing database migration**

Back up the current database, start the application once, and verify schema
version 3, new tables, new indexes, and the original device/scan counts. Do not
delete or rewrite existing data.

- [ ] **Step 5: Restart the local service**

Stop only the exact Uvicorn process for this project, restart one process on
`127.0.0.1:8000`, and verify dashboard, devices, task API, batch API, and cluster
topology return HTTP success responses.

- [ ] **Step 6: Perform real-browser UI verification**

Verify:

- All-device and cluster batch controls render in Chinese.
- A batch progresses from waiting/running to completed.
- Single-device scan polling works.
- Excel first-scan progress appears.
- Device and cluster topology modes still switch correctly.
- No application JavaScript error appears.
