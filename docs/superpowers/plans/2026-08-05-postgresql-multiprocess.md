# PostgreSQL 15 Multi-Process Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SQLite with PostgreSQL 15.18 and run a crash-recoverable multi-Uvicorn distributed scan/import queue with global concurrency limits.

**Architecture:** PostgreSQL is the only runtime database. Alembic owns schema creation, PostgreSQL advisory locks and `FOR UPDATE SKIP LOCKED` coordinate multiple application processes, leases protect in-flight work, and Psycopg notifications invalidate per-process topology caches. Uvicorn worker count is CPU-derived while scan and import limits remain global across all workers.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy 2, Psycopg 3, PostgreSQL 15.18, Alembic, APScheduler, pytest, Ruff, PowerShell.

## Global Constraints

- PostgreSQL 15.x is the only supported runtime database; reject every SQLite URL.
- Start from an empty PostgreSQL database; do not migrate existing SQLite data.
- Use `postgresql+psycopg://` URLs and `psycopg[binary]>=3.3,<4`.
- Use Alembic for all schema creation and upgrades; application workers never call `create_all` or run migrations.
- Default web workers are `min(os.cpu_count() or 1, 8)` and may be overridden by `WEB_WORKERS`.
- `SCAN_MAX_WORKERS=30` and `IMPORT_TEST_MAX_WORKERS=20` are application-wide limits, not per-process limits.
- Remote SSH/WinRM waits never hold database transactions or checked-out database connections.
- Retry only PostgreSQL SQLSTATE `40P01` and `40001`, always with a fresh session.
- Never log passwords, encrypted passwords, complete database URLs, SQL parameters, or imported raw credentials.
- Keep the topology cache TTL at 30 seconds as a fallback for missed notifications.
- Run all database integration tests against a real local PostgreSQL 15 server.

---

### Task 1: Install and provision local PostgreSQL 15

**Files:**
- Modify locally only: `.env` (Git ignored)
- No tracked source changes.

**Interfaces:**
- Produces local services `connection_topology` and `connection_topology_test` reachable on `127.0.0.1:5432`.
- Produces `DATABASE_URL` and `TEST_DATABASE_URL` environment values consumed by every later task.

- [ ] **Step 1: Verify PostgreSQL 15.18 is available from the package source**

```powershell
winget source update
winget show --exact --id PostgreSQL.PostgreSQL.15 --versions
```

Expected: the version list contains `15.18-1`. If it does not, stop and use the PostgreSQL Windows page linked from the official PostgreSQL download site to obtain the EDB-certified 15.18 installer; do not silently install a different major version.

- [ ] **Step 2: Install and provision PostgreSQL in one uninterrupted secure execution unit**

Run Steps 2 through 5 as one orchestration unit so generated values are retained in memory. Generate
both passwords once, never render them in commentary or command output, and keep them only in the
secure execution state until `.env` has been updated with `apply_patch`. Do not split these steps into
independent PowerShell processes.

```powershell
$pgAlphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#%_-'
$pgAdminPassword = -join (1..32 | ForEach-Object { $pgAlphabet[(Get-Random -Maximum $pgAlphabet.Length)] })
winget install --exact --id PostgreSQL.PostgreSQL.15 --version 15.18-1 `
  --accept-package-agreements --accept-source-agreements --silent `
  --override "--mode unattended --superpassword $pgAdminPassword --serverport 5432"
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL 15.18 installation failed' }
```

Expected: Windows service `postgresql-x64-15` is running. Keep `$pgAdminPassword` only in the same
execution unit until the application role is created.

- [ ] **Step 3: Locate `psql` and create development roles/databases without printing secrets**

```powershell
$psql = (Get-ChildItem 'C:\Program Files\PostgreSQL\15\bin\psql.exe' -ErrorAction Stop).FullName
$pgAppPassword = -join (1..40 | ForEach-Object { $pgAlphabet[(Get-Random -Maximum $pgAlphabet.Length)] })
$env:PGPASSWORD = $pgAdminPassword
& $psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 `
  -v app_password="$pgAppPassword" -c "CREATE ROLE connection_topology_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;"
& $psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 `
  -c 'CREATE DATABASE connection_topology OWNER connection_topology_app;'
& $psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 `
  -c 'CREATE DATABASE connection_topology_test OWNER connection_topology_app;'
Remove-Item Env:PGPASSWORD
```

Expected: both databases exist and are owned by the non-superuser application role. If rerunning after a partial install, query `pg_roles` and `pg_database` first and create only missing objects.

- [ ] **Step 4: URL-encode the password and write ignored local settings**

```powershell
$encodedPassword = [System.Uri]::EscapeDataString($pgAppPassword)
$envLines = Get-Content -LiteralPath '.env' -ErrorAction SilentlyContinue | Where-Object {
  $_ -notmatch '^(DATABASE_URL|TEST_DATABASE_URL)='
}
$envLines += "DATABASE_URL=postgresql+psycopg://connection_topology_app:$encodedPassword@127.0.0.1:5432/connection_topology"
$envLines += "TEST_DATABASE_URL=postgresql+psycopg://connection_topology_app:$encodedPassword@127.0.0.1:5432/connection_topology_test"
```

Pass the resolved `$envLines` content directly to the `apply_patch` tool without rendering it to the
user; do not use a shell file-write command and do not print either password. Preserve every unrelated
existing `.env` line, especially `APP_SECRET_KEY`. Expected: `.env` remains ignored by Git.

- [ ] **Step 5: Verify server version and application-role connectivity**

```powershell
$env:PGPASSWORD = $pgAppPassword
& $psql -h 127.0.0.1 -U connection_topology_app -d connection_topology -tAc 'SHOW server_version;'
& $psql -h 127.0.0.1 -U connection_topology_app -d connection_topology_test -tAc 'SELECT current_user, current_database();'
Remove-Item Env:PGPASSWORD
```

Expected: version starts with `15.18`; user is `connection_topology_app`; database is `connection_topology_test`.

---

### Task 2: Make configuration and the SQLAlchemy engine PostgreSQL-only

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/config.py`
- Modify: `app/database.py`
- Create: `app/runtime.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py`
- Modify: `tests/test_database.py`
- Modify: `tests/test_database_pool.py`
- Create: `tests/test_runtime.py`

**Interfaces:**
- Produces `Settings.database_url` validated as `postgresql+psycopg`.
- Produces `Settings.web_workers: int | None`, `db_pool_recycle_seconds`, `scan_lease_seconds`, and `task_heartbeat_seconds`.
- Produces `resolve_web_workers(configured: int | None, cpu_count: int | None = None) -> int`.
- Produces `create_database_engine(settings: Settings) -> Engine` with PostgreSQL health settings.

- [ ] **Step 1: Add failing PostgreSQL configuration tests**

Add to `tests/test_config.py`:

```python
@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./old.db",
        "postgresql://user:pass@localhost/app",
        "mysql+pymysql://user:pass@localhost/app",
    ],
)
def test_settings_accept_only_psycopg_postgresql_urls(valid_key, url):
    with pytest.raises(ValidationError):
        Settings(app_secret_key=valid_key, database_url=url, _env_file=None)


def test_postgresql_defaults(valid_key):
    settings = Settings(
        app_secret_key=valid_key,
        database_url="postgresql+psycopg://app:secret@localhost/app",
        _env_file=None,
    )
    assert settings.web_workers is None
    assert settings.db_pool_size == 3
    assert settings.db_max_overflow == 2
    assert settings.db_pool_timeout_seconds == 30
    assert settings.db_pool_recycle_seconds == 1800
    assert settings.scan_lease_seconds == 90
    assert settings.task_heartbeat_seconds == 15
    assert not hasattr(settings, "sqlite_busy_timeout_ms")
    assert not hasattr(settings, "sqlite_write_retry_delays")
```

Create `tests/test_runtime.py`:

```python
from app.runtime import resolve_web_workers


def test_worker_count_uses_override_or_capped_cpu():
    assert resolve_web_workers(3, cpu_count=64) == 3
    assert resolve_web_workers(None, cpu_count=1) == 1
    assert resolve_web_workers(None, cpu_count=6) == 6
    assert resolve_web_workers(None, cpu_count=64) == 8


def test_worker_count_falls_back_to_one_when_cpu_is_unknown(monkeypatch):
    monkeypatch.setattr("app.runtime.os.cpu_count", lambda: None)
    assert resolve_web_workers(None) == 1
```

- [ ] **Step 2: Run configuration tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_runtime.py -q
```

Expected: FAIL because PostgreSQL-only validation and worker resolution do not exist.

- [ ] **Step 3: Replace SQLite settings and add Psycopg/Alembic dependencies**

In `pyproject.toml`, add:

```toml
"psycopg[binary]>=3.3,<4",
"alembic>=1.16,<2",
```

In `app/config.py`, use these fields and validators:

```python
from sqlalchemy.engine import make_url

database_url: str
web_workers: int | None = Field(default=None, ge=1, le=64)
db_pool_size: int = Field(default=3, ge=1, le=50)
db_max_overflow: int = Field(default=2, ge=0, le=50)
db_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
db_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
scan_lease_seconds: int = Field(default=90, ge=30, le=600)
task_heartbeat_seconds: int = Field(default=15, ge=5, le=120)

@field_validator("database_url")
@classmethod
def require_postgresql_psycopg(cls, value: str) -> str:
    url = make_url(value)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL 必须使用 postgresql+psycopg")
    return value

@model_validator(mode="after")
def heartbeat_precedes_lease(self):
    if self.task_heartbeat_seconds * 2 >= self.scan_lease_seconds:
        raise ValueError("TASK_HEARTBEAT_SECONDS 必须小于扫描租约的一半")
    return self
```

Create `app/runtime.py`:

```python
import os


def resolve_web_workers(
    configured: int | None,
    *,
    cpu_count: int | None = None,
) -> int:
    if configured is not None:
        return configured
    detected = os.cpu_count() if cpu_count is None else cpu_count
    return min(max(detected or 1, 1), 8)
```

- [ ] **Step 4: Replace engine creation and its tests**

Change `app/database.py` to:

```python
def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
```

Delete SQLite PRAGMA listeners. Rewrite `tests/test_database.py` and
`tests/test_database_pool.py` to monkeypatch `app.database.create_engine` and assert the exact
PostgreSQL keyword arguments without opening a real connection.

- [ ] **Step 5: Install dependencies and run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_runtime.py tests/test_database.py tests/test_database_pool.py -q
```

Expected: PASS.

- [ ] **Step 6: Update `.env.example` and commit**

Use:

```dotenv
DATABASE_URL=postgresql+psycopg://connection_topology_app:change-me@127.0.0.1:5432/connection_topology
WEB_WORKERS=
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1800
SCAN_MAX_WORKERS=30
IMPORT_TEST_MAX_WORKERS=20
SCAN_LEASE_SECONDS=90
TASK_HEARTBEAT_SECONDS=15
```

Remove `SQLITE_BUSY_TIMEOUT_MS` and `SQLITE_WRITE_RETRY_DELAYS`.

```powershell
git add pyproject.toml app/config.py app/database.py app/runtime.py .env.example tests/test_config.py tests/test_runtime.py tests/test_database.py tests/test_database_pool.py
git commit -m "feat: configure postgresql runtime"
```

---

### Task 3: Replace custom migrations with an Alembic PostgreSQL schema

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260805_0001_postgresql_initial.py`
- Modify: `app/models.py`
- Modify: `app/database.py`
- Delete: `app/migrations.py`
- Modify: `tests/conftest.py`
- Replace: `tests/test_migrations.py`

**Interfaces:**
- Produces Alembic revision `20260805_0001` as the single schema head.
- Produces `assert_database_current(engine: Engine) -> None`.
- Adds scan and import lease columns consumed by Tasks 5–7.

- [ ] **Step 1: Add PostgreSQL test database fixtures**

In `tests/conftest.py`, require `TEST_DATABASE_URL`, validate that its driver is
`postgresql+psycopg`, and add a session-scoped engine. Run `alembic upgrade head` before yielding.
Before each ordinary test, truncate all business tables with `RESTART IDENTITY CASCADE`, then
insert `SystemSetting(id=1, history_retention_days=7)`. Change the existing `app` fixture to use
`test_database_url` and remove `init_database`. Mark migration tests with `@pytest.mark.migration`
so the truncation fixture skips them; the migration fixture must restore `upgrade head` in a
`finally` block.

Add to `pyproject.toml`:

```toml
markers = [
  "migration: changes the PostgreSQL test database schema",
]
```

- [ ] **Step 2: Add failing model and migration tests**

Rewrite `tests/test_migrations.py` with:

```python
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.database import assert_database_current


def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


@pytest.mark.migration
def test_empty_postgresql_database_upgrades_to_head(test_database_url, migrated_engine):
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        script = ScriptDirectory.from_config(config)
        with migrated_engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == script.get_current_head()
            tables = set(inspect(connection).get_table_names())
            assert {"devices", "scan_tasks", "scan_runs", "import_row_results"} <= tables
            columns = {column["name"] for column in inspect(connection).get_columns("scan_tasks")}
            assert {"worker_id", "lease_expires_at", "heartbeat_at", "attempt_count"} <= columns
    finally:
        command.upgrade(config, "head")


def test_active_scan_task_index_is_partial_postgresql_index(migrated_engine):
    with migrated_engine.connect() as connection:
        definition = connection.scalar(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname='uq_scan_tasks_device_active'"
        ))
    assert "WHERE" in definition
    assert "PENDING" in definition and "RUNNING" in definition


@pytest.mark.migration
def test_database_version_check_rejects_unmigrated_engine(
    test_database_url,
    migrated_engine,
):
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "base")
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            assert_database_current(migrated_engine)
    finally:
        command.upgrade(config, "head")
```

- [ ] **Step 3: Run migration tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py -q
```

Expected: FAIL because Alembic files and lease fields do not exist.

- [ ] **Step 4: Add lease fields and PostgreSQL index metadata**

In `app/models.py`, add to `ScanTask`:

```python
worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
```

Add the same fields prefixed with `test_` to `ImportRowResult`:

```python
test_worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
test_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
test_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
test_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
```

Replace the SQLite predicate on `uq_scan_tasks_device_active` with the PostgreSQL predicate:

```python
postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
```

- [ ] **Step 5: Create Alembic configuration and initial migration**

Configure `migrations/env.py` to import `Base.metadata`, read `DATABASE_URL` through
`Settings`, enable `compare_type=True`, and support online migrations only. The initial revision
must create every table from `app/models.py`, PostgreSQL enum types, all foreign keys, and all
indexes, including the partial active-task index. Use `sa.DateTime(timezone=True)` for every
timestamp and `sa.true()`/`sa.false()` for Boolean defaults.

Set these exact revision identifiers:

```python
revision = "20260805_0001"
down_revision = None
```

The downgrade must drop tables in reverse dependency order, then drop PostgreSQL enum types.

- [ ] **Step 6: Replace application migration entry point**

Delete `init_database`. Add to `app/database.py`:

```python
def assert_database_current(engine: Engine) -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != script.get_current_head():
        raise RuntimeError("数据库结构未升级，请先执行 alembic upgrade head")
```

- [ ] **Step 7: Reset the test database, migrate, and run tests**

```powershell
.\.venv\Scripts\python.exe -m alembic downgrade base
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py -q
.\.venv\Scripts\python.exe -m alembic check
```

Expected: migration tests PASS and Alembic reports no new upgrade operations.

- [ ] **Step 8: Commit**

```powershell
git add alembic.ini migrations app/models.py app/database.py pyproject.toml tests/conftest.py tests/test_migrations.py
git rm app/migrations.py
git commit -m "feat: manage postgresql schema with alembic"
```

---

### Task 4: Replace SQLite write coordination with PostgreSQL transaction execution

**Files:**
- Create: `app/services/database_transactions.py`
- Delete: `app/services/sqlite_writes.py`
- Replace: `tests/test_sqlite_writes.py` with `tests/test_database_transactions.py`
- Modify: imports in `app/main.py`, `app/routes/api.py`, `app/services/scan_queue.py`, `app/services/import_testing.py`, `app/services/imports.py`, `app/services/scheduler.py`

**Interfaces:**
- Produces `DatabaseUnavailable`, `TransactionConflict`, and `PostgresTransactionRunner`.
- Produces `run(operation_name: str, operation: Callable[[Session], T]) -> T`.
- Produces `guard(operation_name: str) -> ContextManager[None]` for request-owned sessions.

- [ ] **Step 1: Add failing SQLSTATE and fresh-session retry tests**

Create `tests/test_database_transactions.py`:

```python
class DriverError(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


def db_error(sqlstate):
    return OperationalError("statement", {}, DriverError(sqlstate))


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
def test_runner_retries_only_transaction_conflicts(app, sqlstate):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0, 0.0))
    attempts = []

    def operation(session):
        attempts.append(id(session))
        if len(attempts) < 3:
            raise db_error(sqlstate)
        return "saved"

    assert runner.run("save", operation) == "saved"
    assert len(set(attempts)) == 3


def test_runner_does_not_retry_unique_violation(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0, 0.0))
    with pytest.raises(IntegrityError):
        runner.run("duplicate", lambda session: (_ for _ in ()).throw(
            IntegrityError("insert", {}, DriverError("23505"))
        ))


def test_retry_exhaustion_is_safe_and_contains_no_sql(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0,))
    with pytest.raises(TransactionConflict, match="事务冲突") as caught:
        runner.run("persist_scan", lambda session: (_ for _ in ()).throw(db_error("40001")))
    assert "statement" not in str(caught.value)


def test_connectivity_failure_maps_to_safe_unavailable_error(app):
    runner = PostgresTransactionRunner(app.state.session_factory, (0.0,))
    with pytest.raises(DatabaseUnavailable, match="暂时不可用") as caught:
        runner.run("load", lambda session: (_ for _ in ()).throw(db_error(None)))
    assert "statement" not in str(caught.value)
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_database_transactions.py -q
```

Expected: FAIL because the PostgreSQL transaction runner does not exist.

- [ ] **Step 3: Implement the runner**

Create `app/services/database_transactions.py` with:

```python
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
DATABASE_UNAVAILABLE_MESSAGE = "数据库暂时不可用，请稍后重试"
TRANSACTION_CONFLICT_MESSAGE = "数据库事务冲突，请重试"


def postgres_sqlstate(exc: Exception) -> str | None:
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None)


class DatabaseUnavailable(RuntimeError):
    pass


class TransactionConflict(RuntimeError):
    pass


class PostgresTransactionRunner:
    def __init__(self, session_factory, retry_delays=(0.05, 0.15, 0.4)):
        self.session_factory = session_factory
        self.retry_delays = tuple(retry_delays)

    def run(self, operation_name, operation):
        for attempt in range(len(self.retry_delays) + 1):
            with self.session_factory() as session:
                try:
                    result = operation(session)
                    session.commit()
                    return result
                except DBAPIError as exc:
                    session.rollback()
                    sqlstate = postgres_sqlstate(exc)
                    if sqlstate not in RETRYABLE_SQLSTATES:
                        if isinstance(exc, OperationalError):
                            raise DatabaseUnavailable(DATABASE_UNAVAILABLE_MESSAGE) from exc
                        raise
                    if attempt == len(self.retry_delays):
                        raise TransactionConflict(TRANSACTION_CONFLICT_MESSAGE) from exc
                    logger.warning(
                        "PostgreSQL 事务重试 operation=%s sqlstate=%s attempt=%s/%s",
                        operation_name,
                        sqlstate,
                        attempt + 1,
                        len(self.retry_delays) + 1,
                    )
                except sqlalchemy.exc.TimeoutError as exc:
                    session.rollback()
                    raise DatabaseUnavailable(DATABASE_UNAVAILABLE_MESSAGE) from exc
            time.sleep(self.retry_delays[attempt])
        raise AssertionError("事务重试循环未返回")
```

Implement `guard()` as a context manager that maps SQLSTATE `40P01/40001` to
`TransactionConflict` and pool/connectivity failures to `DatabaseUnavailable`, without replaying
the caller-owned session.

- [ ] **Step 4: Replace dependency names and error mappings**

Rename constructor parameters from `write_coordinator` to `transaction_runner`. Replace `.write`
with `.run` and `.write_once` with `.guard`. In `app/routes/api.py`, map both safe exceptions to
HTTP 503. Remove `database_busy` creation and replace final scan failures with
`transaction_conflict` only when a replayable persistence transaction exhausts retries.

- [ ] **Step 5: Run transaction and existing service tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_database_transactions.py tests/test_scan_queue.py tests/test_import_testing.py tests/test_imports.py tests/test_scheduler_queue.py tests/test_api.py -q
```

Expected: PASS with no references to `SQLiteWriteCoordinator` or `DatabaseBusy`.

- [ ] **Step 6: Commit**

```powershell
git add app tests
git rm app/services/sqlite_writes.py tests/test_sqlite_writes.py
git commit -m "feat: execute postgresql transactions safely"
```

---

### Task 5: Add PostgreSQL scan claiming and global concurrency leases

**Files:**
- Create: `app/services/task_leases.py`
- Modify: `app/services/scan_queue.py`
- Create: `tests/test_task_leases.py`
- Modify: `tests/test_scan_queue.py`

**Interfaces:**
- Produces `SCAN_CLAIM_LOCK_KEY = 740_001`.
- Produces `claim_scan_tasks(session, worker_id, local_capacity, global_limit, lease_seconds) -> list[int]`.
- Produces `renew_scan_leases(session, worker_id, task_ids, lease_seconds) -> set[int]` returning lost IDs.
- Produces `TaskLeaseLost(task_id: int)`.

- [ ] **Step 1: Add failing two-claimer and expired-lease tests**

Create `tests/test_task_leases.py` with real PostgreSQL sessions:

```python
def test_two_workers_never_claim_same_task_and_respect_global_limit(app):
    device_ids = seed_devices(app, 40)
    seed_pending_tasks(app, device_ids)
    barrier = threading.Barrier(2)

    def claim(worker_id):
        barrier.wait()
        return app.state.transaction_runner.run(
            f"claim_{worker_id}",
            lambda session: claim_scan_tasks(session, worker_id, 30, 30, 90),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in (
            pool.submit(claim, "worker-a"), pool.submit(claim, "worker-b")
        )]
    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 30


def test_expired_task_is_requeued_and_claimed_by_new_worker(app):
    task_id = seed_running_task(app, worker_id="dead", lease_delta_seconds=-1)
    claimed = app.state.transaction_runner.run(
        "claim_recovered",
        lambda session: claim_scan_tasks(session, "worker-new", 1, 30, 90),
    )
    assert claimed == [task_id]
    with app.state.session_factory() as session:
        task = session.get(ScanTask, task_id)
        assert task.worker_id == "worker-new"
        assert task.attempt_count == 2
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_leases.py -q
```

Expected: FAIL because lease claiming is absent.

- [ ] **Step 3: Implement advisory-locked claiming**

In `app/services/task_leases.py`:

```python
SCAN_CLAIM_LOCK_KEY = 740_001


def claim_scan_tasks(session, worker_id, local_capacity, global_limit, lease_seconds):
    if local_capacity <= 0:
        return []
    session.execute(select(func.pg_advisory_xact_lock(SCAN_CLAIM_LOCK_KEY)))
    now = session.scalar(select(func.now()))
    expired = session.scalars(
        select(ScanTask)
        .where(
            ScanTask.status == ScanTaskStatus.RUNNING,
            ScanTask.lease_expires_at <= now,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for task in expired:
        reset_expired_scan_task(session, task)
    session.flush()
    active = session.scalar(
        select(func.count()).select_from(ScanTask).where(
            ScanTask.status == ScanTaskStatus.RUNNING,
            ScanTask.lease_expires_at > now,
        )
    ) or 0
    claim_limit = min(local_capacity, max(global_limit - active, 0))
    tasks = session.scalars(
        select(ScanTask)
        .where(ScanTask.status == ScanTaskStatus.PENDING)
        .order_by(ScanTask.priority.desc(), ScanTask.created_at, ScanTask.id)
        .with_for_update(skip_locked=True)
        .limit(claim_limit)
    ).all()
    lease_until = now + timedelta(seconds=lease_seconds)
    for task in tasks:
        task.status = ScanTaskStatus.RUNNING
        task.worker_id = worker_id
        task.started_at = task.started_at or now
        task.heartbeat_at = now
        task.lease_expires_at = lease_until
        task.attempt_count += 1
        mark_scan_batch_items_running(task)
    refresh_scan_batches(session, {item.batch_id for task in tasks for item in task.items})
    return [task.id for task in tasks]
```

`reset_expired_scan_task` clears ownership/lease fields and restores linked batch items to
`PENDING`. `refresh_scan_batches` owns the batch-count logic moved out of `ScanQueueService`.

- [ ] **Step 4: Implement batched heartbeat renewal**

Use one `UPDATE ... RETURNING id` for matching `worker_id`, `RUNNING` status, and unexpired
leases. Return `requested_ids - renewed_ids` as lost IDs. Never let an expired worker revive its
own lease.

- [ ] **Step 5: Replace `_claim_tasks` in `ScanQueueService`**

Give each queue `worker_id: str = uuid.uuid4().hex`. Call `claim_scan_tasks` through
`transaction_runner.run("claim_scan_tasks", ...)`. Remove the process-local `_claim_lock` and
startup-wide recovery that resets every `RUNNING` task. The executor may have 30 local threads,
but the claim transaction enforces the global limit.

- [ ] **Step 6: Run claim and queue tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_leases.py tests/test_scan_queue.py tests/test_scan_queue_api.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/services/task_leases.py app/services/scan_queue.py tests/test_task_leases.py tests/test_scan_queue.py
git commit -m "feat: claim scans across postgresql workers"
```

---

### Task 6: Add scan heartbeats and ownership-safe atomic persistence

**Files:**
- Modify: `app/services/scan_queue.py`
- Modify: `app/services/task_leases.py`
- Modify: `tests/test_task_leases.py`
- Create: `tests/test_scan_lease_recovery.py`

**Interfaces:**
- Consumes `renew_scan_leases` and `TaskLeaseLost` from Task 5.
- Produces a queue heartbeat thread renewing all locally active task IDs in one transaction.
- Guarantees scan persistence requires the same `worker_id` that owns the live lease.

- [ ] **Step 1: Add failing lease-loss persistence test**

Create `tests/test_scan_lease_recovery.py`:

```python
def test_old_worker_cannot_persist_after_new_worker_reclaims_task(app):
    device_id, task_id = seed_expired_scan(app, worker_id="old")
    claimed = app.state.transaction_runner.run(
        "reclaim",
        lambda session: claim_scan_tasks(session, "new", 1, 30, 90),
    )
    assert claimed == [task_id]
    outcome = successful_outcome(device_id)
    old_queue = make_queue(app, worker_id="old")
    with pytest.raises(TaskLeaseLost):
        app.state.transaction_runner.run(
            "persist_old",
            lambda session: old_queue._persist_outcome(session, task_id, outcome),
        )
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 0
```

- [ ] **Step 2: Add failing heartbeat test**

```python
def test_heartbeat_keeps_long_remote_collection_owned(app):
    collector = BlockingCollector()
    queue = make_queue(app, worker_id="alive", heartbeat_seconds=1, lease_seconds=4)
    task_id = seed_and_claim(app, queue)
    queue.start()
    try:
        collector.started.wait(3)
        time.sleep(5)
        with app.state.session_factory() as session:
            task = session.get(ScanTask, task_id)
            assert task.worker_id == "alive"
            assert task.lease_expires_at > session.scalar(select(func.now()))
    finally:
        collector.release.set()
        queue.shutdown()
```

- [ ] **Step 3: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scan_lease_recovery.py -q
```

Expected: FAIL because ownership is not checked and heartbeats are absent.

- [ ] **Step 4: Require ownership during persistence**

At the beginning of `_persist_outcome`, lock and load with:

```python
task = session.scalar(
    select(ScanTask).where(
        ScanTask.id == task_id,
        ScanTask.status == ScanTaskStatus.RUNNING,
        ScanTask.worker_id == self.worker_id,
        ScanTask.lease_expires_at > func.now(),
    ).with_for_update()
)
if task is None:
    raise TaskLeaseLost(task_id)
```

On success or collector failure, clear `worker_id`, lease, and heartbeat in the same transaction
that creates `ScanRun`, connections, device state, task state, batch items, and batch counts.

- [ ] **Step 5: Add the batched heartbeat loop**

Maintain `_active_task_ids` behind a lock. A single daemon thread wakes every
`task_heartbeat_seconds`, calls `renew_scan_leases` for all active IDs, and marks returned lost IDs
in `_lost_task_ids`. `_execute_task` checks the lost set before persistence and raises
`TaskLeaseLost`. During shutdown, stop claiming first, keep heartbeats running until executor work
finishes, then stop and join the heartbeat thread.

- [ ] **Step 6: Classify lease loss without overwriting the new owner**

`_execute_safely` logs operation name, task ID, and worker ID at INFO for `TaskLeaseLost`, then
returns without calling the generic task failure writer. Other unexpected failures may only fail a
task when the current worker still owns its live lease.

- [ ] **Step 7: Run scan persistence and recovery tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scan_lease_recovery.py tests/test_scan_queue.py tests/test_scan_write_concurrency.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add app/services/scan_queue.py app/services/task_leases.py tests/test_task_leases.py tests/test_scan_lease_recovery.py tests/test_scan_write_concurrency.py
git commit -m "feat: recover leased scan tasks safely"
```

---

### Task 7: Distribute import connection tests across processes

**Files:**
- Create: `app/services/import_test_leases.py`
- Modify: `app/services/import_testing.py`
- Modify: `app/services/imports.py`
- Modify: `app/main.py`
- Modify: `tests/test_import_testing.py`
- Create: `tests/test_import_test_leases.py`

**Interfaces:**
- Produces `IMPORT_TEST_CLAIM_LOCK_KEY = 740_002`.
- Produces `claim_import_tests`, `renew_import_test_leases`, and `ImportTestLeaseLost`.
- Changes `ImportTestService.start()` into a persistent dispatcher used by every app process.

- [ ] **Step 1: Add failing distributed import claim tests**

Create `tests/test_import_test_leases.py`:

```python
def test_two_import_workers_respect_global_twenty_and_do_not_overlap(app):
    row_ids = seed_pending_import_rows(app, 30)
    barrier = threading.Barrier(2)

    def claim(worker):
        barrier.wait()
        return app.state.transaction_runner.run(
            f"claim_{worker}",
            lambda session: claim_import_tests(session, worker, 20, 20, 90),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in (
            pool.submit(claim, "import-a"), pool.submit(claim, "import-b")
        )]
    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 20
    assert set(first + second) <= set(row_ids)


def test_expired_import_test_is_reclaimed_and_old_result_is_rejected(app):
    row_id = seed_expired_import_test(app, "dead")
    claimed = claim_with_runner(app, "new", 1)
    assert claimed == [row_id]
    with pytest.raises(ImportTestLeaseLost):
        save_result_as(app, row_id, "dead", ImportTestStatus.SUCCESS)
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_import_test_leases.py -q
```

Expected: FAIL because import leases do not exist.

- [ ] **Step 3: Implement import claim and heartbeat functions**

Mirror the scan algorithm with import-specific columns and advisory key `740_002`. Count rows with
`test_status=RUNNING` and an unexpired `test_lease_expires_at`; reclaim expired rows; select
`PENDING` rows using `FOR UPDATE SKIP LOCKED`; set worker, lease, heartbeat, and increment attempt.
Refresh all affected `ImportBatch` counts in the same transaction.

- [ ] **Step 4: Replace request-local submission with persistent dispatch**

Every `ImportTestService` receives its own `worker_id`, starts a polling dispatcher and one batched
heartbeat thread, and claims only up to local executor capacity. `import_devices` only commits
`PENDING` rows and wakes the local dispatcher; any process may claim them. Remove
`schedule_batch()` Future creation and startup logic that resets all `RUNNING` rows.

- [ ] **Step 5: Protect result persistence by lease owner**

`_save_result` must select the row with matching `test_worker_id`, `RUNNING` status, and live lease
using `FOR UPDATE`. On mismatch raise `ImportTestLeaseLost` without changing row or batch. On
success/failure clear all test lease fields and refresh batch counts atomically.

- [ ] **Step 6: Run import suites**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_import_test_leases.py tests/test_import_testing.py tests/test_imports.py tests/test_import_frontend.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/services/import_test_leases.py app/services/import_testing.py app/services/imports.py app/main.py tests/test_import_test_leases.py tests/test_import_testing.py tests/test_imports.py
git commit -m "feat: distribute import tests with postgresql leases"
```

---

### Task 8: Elect one scheduler leader with PostgreSQL advisory locks

**Files:**
- Create: `app/services/postgres_leader.py`
- Modify: `app/services/scheduler.py`
- Modify: `app/main.py`
- Create: `tests/test_postgres_leader.py`
- Modify: `tests/test_scheduler_queue.py`

**Interfaces:**
- Produces `PostgresLeaderElector(engine, lock_key, on_acquired, on_lost, retry_seconds=2.0)`.
- Produces `SCHEDULER_LEADER_LOCK_KEY = 740_003`.
- `SchedulerService.start()` starts election; APScheduler starts only after acquisition.

- [ ] **Step 1: Add failing exclusive-leader and failover tests**

Create `tests/test_postgres_leader.py`:

```python
def test_only_one_candidate_holds_scheduler_lock_and_second_takes_over(migrated_engine):
    events = []
    first = PostgresLeaderElector(
        migrated_engine, 740_003,
        lambda: events.append("first-acquired"),
        lambda: events.append("first-lost"),
        retry_seconds=0.05,
    )
    second = PostgresLeaderElector(
        migrated_engine, 740_003,
        lambda: events.append("second-acquired"),
        lambda: events.append("second-lost"),
        retry_seconds=0.05,
    )
    first.start()
    second.start()
    wait_until(lambda: len([e for e in events if e.endswith("acquired")]) == 1)
    first.shutdown()
    wait_until(lambda: "second-acquired" in events)
    second.shutdown()
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_postgres_leader.py -q
```

Expected: FAIL because the elector does not exist.

- [ ] **Step 3: Implement a dedicated-connection elector**

The election thread opens `engine.connect()`, sets `AUTOCOMMIT`, and executes:

```sql
SELECT pg_try_advisory_lock(:lock_key)
```

Keep the exact connection open for the leadership lifetime. Poll `SELECT 1` to detect broken
connections. On connection loss call `on_lost`, close it, and retry. On clean shutdown execute
`SELECT pg_advisory_unlock(:lock_key)`, close, and call `on_lost` once. Serialize callbacks so
acquisition and loss cannot overlap.

- [ ] **Step 4: Integrate SchedulerService**

Split scheduler lifecycle into `_become_leader()` and `_lose_leadership()`. The former starts
APScheduler, loads enabled devices, and schedules jobs. The latter shuts APScheduler down without
waiting. All Uvicorn processes call `SchedulerService.start()`, but only the elected process runs
jobs.

- [ ] **Step 5: Run scheduler tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_postgres_leader.py tests/test_scheduler_queue.py tests/test_retention.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/services/postgres_leader.py app/services/scheduler.py app/main.py tests/test_postgres_leader.py tests/test_scheduler_queue.py
git commit -m "feat: elect one postgresql scheduler leader"
```

---

### Task 9: Invalidate topology caches across all workers

**Files:**
- Create: `app/services/postgres_notifications.py`
- Modify: `app/services/topology_cache.py`
- Modify: `app/services/scan_queue.py`
- Modify: `app/services/imports.py`
- Modify: `app/services/scheduler.py`
- Modify: `app/routes/api.py`
- Modify: `app/main.py`
- Create: `tests/test_postgres_notifications.py`
- Modify: `tests/test_topology_cache.py`

**Interfaces:**
- Produces `TOPOLOGY_CHANNEL = "topology_changed"`.
- Produces `notify_topology_changed(session: Session) -> None`.
- Produces `PostgresNotificationListener(database_url, channel, callback, retry_seconds=1.0)`.

- [ ] **Step 1: Add failing cross-connection notification test**

Create `tests/test_postgres_notifications.py`:

```python
def test_committed_notification_clears_another_worker_cache(app):
    received = threading.Event()
    listener = PostgresNotificationListener(
        app.state.settings.database_url,
        TOPOLOGY_CHANNEL,
        received.set,
        retry_seconds=0.05,
    )
    listener.start()
    try:
        app.state.transaction_runner.run(
            "notify_test",
            lambda session: notify_topology_changed(session),
        )
        assert received.wait(3)
    finally:
        listener.shutdown()


def test_rolled_back_notification_is_not_delivered(app):
    received = threading.Event()
    listener = make_listener(app, received.set)
    listener.start()
    try:
        with app.state.session_factory() as session:
            notify_topology_changed(session)
            session.rollback()
        assert received.wait(0.3) is False
    finally:
        listener.shutdown()
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_postgres_notifications.py -q
```

Expected: FAIL because notification support is absent.

- [ ] **Step 3: Implement publisher and listener**

Publisher:

```python
def notify_topology_changed(session: Session) -> None:
    session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": TOPOLOGY_CHANNEL})
```

Listener converts the SQLAlchemy URL to a plain PostgreSQL URI with
`make_url(database_url).set(drivername="postgresql")`, opens a Psycopg autocommit connection,
executes `LISTEN topology_changed`, waits through `connection.notifies(timeout=1)`, and calls the
callback. On operational failure it closes, waits, and reconnects. It logs only host-independent
operation names.

- [ ] **Step 4: Publish inside every cache-affecting transaction**

Call `notify_topology_changed(session)` before commit in successful scan persistence, device and
cluster mutations, import mutations that add devices/clusters, and history purge. Keep direct local
cache clearing after commit for immediate same-process behavior.

- [ ] **Step 5: Start and stop one listener per app process**

Construct the listener in `create_app`; start it after database version verification; shut it down
before engine disposal. Callback is `app.state.topology_cache.clear`. Retain TTL 30.

- [ ] **Step 6: Run notification and topology suites**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_postgres_notifications.py tests/test_topology_cache.py tests/test_topology_history.py tests/test_topology_normalization.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/services/postgres_notifications.py app/services/topology_cache.py app/services/scan_queue.py app/services/imports.py app/services/scheduler.py app/routes/api.py app/main.py tests/test_postgres_notifications.py tests/test_topology_cache.py
git commit -m "feat: invalidate topology cache across workers"
```

---

### Task 10: Remove SQLite lifecycle code and add PostgreSQL preflight/multi-worker startup

**Files:**
- Delete: `app/services/process_guard.py`
- Create: `app/preflight.py`
- Modify: `app/main.py`
- Modify: `app/routes/api.py`
- Modify: `start.ps1`
- Delete: `tests/test_process_guard.py`
- Modify: `tests/test_main.py`
- Create: `tests/test_preflight.py`
- Create: `tests/test_health.py`

**Interfaces:**
- Produces `run_preflight(settings: Settings, workers: int) -> PreflightReport`.
- Produces `/api/health` returning database and migration readiness.
- `start.ps1` runs Alembic once, preflight once, then starts CPU-derived Uvicorn workers.

- [ ] **Step 1: Add failing connection-budget and health tests**

Create `tests/test_preflight.py`:

```python
def test_preflight_calculates_total_connection_budget(app, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "load_postgresql_limits",
        lambda engine: PostgreSQLLimits(server_version="15.18", max_connections=100),
    )
    report = run_preflight(app.state.settings, workers=8)
    assert report.requested_connections == 8 * (3 + 2 + 2)
    assert report.available_connections == 90


def test_preflight_rejects_excessive_connection_budget(app, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "load_postgresql_limits",
        lambda engine: PostgreSQLLimits(server_version="15.18", max_connections=40),
    )
    with pytest.raises(RuntimeError, match="连接数预算"):
        run_preflight(app.state.settings, workers=8)
```

Create `tests/test_health.py` asserting `/api/health` returns `200` with
`{"database":"ok","migration":"current"}` and returns `503` when `SELECT 1` fails.

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_preflight.py tests/test_health.py tests/test_main.py -q
```

Expected: FAIL because preflight and health do not exist.

- [ ] **Step 3: Implement preflight**

`load_postgresql_limits` queries:

```sql
SELECT current_setting('server_version'), current_setting('max_connections')::integer
```

Reject non-15 major versions. Calculate reserved listener/leader connections as two per worker and
requested connections as `workers * (pool_size + max_overflow + 2)`. Keep 10 server connections
reserved. Call `assert_database_current` and return a frozen report dataclass.

- [ ] **Step 4: Simplify application lifespan**

Remove `SQLiteProcessGuard`, `init_database`, and SQLite coordinator state. Lifespan order:

1. `assert_database_current(engine)`.
2. Initialize the singleton `SystemSetting` through `PostgresTransactionRunner`.
3. Start scan queue, import test dispatcher, scheduler election, and notification listener.
4. Yield.
5. Shut down scheduler election, import dispatcher, scan queue, and notification listener.
6. Dispose engine.

- [ ] **Step 5: Add health route**

The health route uses a short new session to execute `SELECT 1` and reads the already verified
migration state. Catch database connectivity exceptions and return HTTP 503 without driver text.

- [ ] **Step 6: Replace `start.ps1`**

Use:

```powershell
$ErrorActionPreference = 'Stop'
$python = '.\.venv\Scripts\python.exe'
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$workers = & $python -c "from app.config import get_settings; from app.runtime import resolve_web_workers; s=get_settings(); print(resolve_web_workers(s.web_workers))"
& $python -m app.preflight --workers $workers
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers $workers
```

- [ ] **Step 7: Run lifecycle tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_preflight.py tests/test_health.py tests/test_main.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add app/preflight.py app/main.py app/routes/api.py start.ps1 tests/test_preflight.py tests/test_health.py tests/test_main.py
git rm app/services/process_guard.py tests/test_process_guard.py
git commit -m "feat: start postgresql multi-worker runtime"
```

---

### Task 11: Convert the entire test harness to real PostgreSQL

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_windows_optional.py`
- Modify: `tests/test_main.py`
- Modify: `tests/test_database.py`
- Modify: `tests/test_database_pool.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/test_scan_write_concurrency.py`
- Create: `tests/test_postgresql_multiprocess.py`

**Interfaces:**
- Produces session-scoped `test_database_url` and `migrated_engine` fixtures.
- Produces per-test PostgreSQL cleanup without relying on a single rollback transaction.
- Verifies two application queue instances share global limits.

- [ ] **Step 1: Audit and finalize the PostgreSQL root fixtures from Task 3**

Confirm `tests/conftest.py` requires `TEST_DATABASE_URL`, validates driver name
`postgresql+psycopg`, runs `alembic upgrade head` once per session, and before each app fixture runs:

```sql
TRUNCATE TABLE
  connection_records,
  scan_batch_items,
  scan_tasks,
  scan_batches,
  scan_runs,
  import_row_results,
  import_batches,
  cluster_internal_networks,
  devices,
  clusters,
  system_settings
RESTART IDENTITY CASCADE
```

Then insert `SystemSetting(id=1, history_retention_days=7)`. Keep cleanup outside a wrapping test
transaction because worker threads use independent sessions. Add a test that unsets
`TEST_DATABASE_URL` and asserts the fixture helper raises a message naming that exact variable.

- [ ] **Step 2: Remove SQLite-only test setup**

Replace the remaining `sqlite:///...` setting in `tests/test_windows_optional.py` with
`test_database_url`. Delete assertions for PRAGMA, SQLite file locks, `database is locked`, and
`database_busy` from the named files above. Retain partial-index coverage through the PostgreSQL
inspector test in `tests/test_migrations.py`.

- [ ] **Step 3: Add a deterministic two-queue global concurrency test**

Create `tests/test_postgresql_multiprocess.py`:

```python
def test_two_queue_instances_share_global_thirty_scan_limit(app):
    tracker = ConcurrencyTracker(release_after=30)
    device_ids = seed_devices(app, 60)
    first = make_queue(app, tracker.collector, worker_id="process-a")
    second = make_queue(app, tracker.collector, worker_id="process-b")
    batch = first.create_batch(ScanBatchType.ALL, device_ids)
    first.start()
    second.start()
    try:
        assert tracker.first_wave_ready.wait(15)
        assert tracker.maximum == 30
        tracker.release_first_wave.set()
        wait_for_batch(app, batch.id, timeout=30)
    finally:
        first.shutdown()
        second.shutdown()
    assert tracker.maximum == 30
    assert load_batch(app, batch.id).success_tasks == 60
```

- [ ] **Step 4: Add a real two-service import limit test**

Seed 40 pending import rows, start two `ImportTestService` instances, block the first 20 collectors,
assert observed maximum is 20, release them, and assert all rows finish once with
`test_attempt_count == 1`.

- [ ] **Step 5: Add a multi-worker Uvicorn smoke test**

Launch a subprocess with:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 2
```

Poll `/api/health` until it returns 200, issue 20 parallel health requests, then stop the process
tree in a `finally` block. Assert every response is healthy and logs contain no migration race or
duplicate-scheduler error.

- [ ] **Step 6: Run all tests once and fix PostgreSQL semantic differences**

```powershell
$env:TEST_DATABASE_URL = (Get-Content .env | Select-String '^TEST_DATABASE_URL=').Line.Split('=',2)[1]
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests PASS. Fix enum casing, timezone-aware datetime assertions, PostgreSQL constraint
names, and explicit ordering in the implementation rather than weakening assertions.

- [ ] **Step 7: Commit**

```powershell
git add tests
git commit -m "test: run full suite on postgresql"
```

---

### Task 12: Document PostgreSQL 15.18 operations and run final acceptance

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Create: `docs/postgresql-15-deployment.md`
- Modify: `pyproject.toml` if dependency pins changed during implementation.

**Interfaces:**
- Documents exact local and intranet deployment, migration, connection budgeting, backup, restore,
  health check, and rollback commands.
- Produces final evidence for PostgreSQL-only multi-process operation.

- [ ] **Step 1: Replace SQLite documentation**

Remove all SQLite startup, file backup, `--workers` prohibition, busy timeout, and file-lock text.
Document `DATABASE_URL`, CPU-derived workers, global concurrency semantics, Alembic, and the
PostgreSQL health endpoint.

- [ ] **Step 2: Write the deployment runbook**

In `docs/postgresql-15-deployment.md`, include:

```sql
CREATE ROLE connection_topology_app LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE connection_topology OWNER connection_topology_app;
```

Include a restricted `pg_hba.conf` example using the actual application host CIDR chosen by the
operator, `scram-sha-256`, `alembic upgrade head`, `start.ps1`, `/api/health`, connection-budget
calculation, `pg_dump -Fc`, `pg_restore --clean --if-exists`, and rollback by restoring the previous
application package plus its matching database backup. Explicitly instruct operators to replace
the example CIDR before deployment and never commit credentials.

- [ ] **Step 3: Run static and migration checks**

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests migrations
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m app.preflight --workers 2
git diff --check
```

Expected: all commands exit 0; preflight reports PostgreSQL 15.18 and a safe connection budget.

- [ ] **Step 4: Run the complete PostgreSQL suite three consecutive times**

```powershell
1..3 | ForEach-Object {
  .\.venv\Scripts\python.exe -m pytest -q
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Expected: every run passes with zero failures.

- [ ] **Step 5: Search for removed SQLite paths and sensitive logging**

```powershell
rg -n "sqlite|SQLite|database is locked|database_busy|SQLiteWriteCoordinator|SQLiteProcessGuard|INSERT OR IGNORE" app tests README.md .env.example docs/postgresql-15-deployment.md
rg -n "password|encrypted_password|DATABASE_URL|sqlstate|params" app tests
```

Expected: the first search only finds an explicit PostgreSQL-only rejection test or historical
design documents outside the searched deployment file; no runtime SQLite path remains. The second
search confirms secrets are accepted/encrypted/redacted but never logged.

- [ ] **Step 6: Verify a real two-worker start**

```powershell
$server = Start-Process -FilePath '.\.venv\Scripts\python.exe' `
  -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8765','--workers','2' `
  -WindowStyle Hidden -PassThru
try {
  1..30 | ForEach-Object {
    try {
      $health = Invoke-RestMethod 'http://127.0.0.1:8765/api/health'
      if ($health.database -eq 'ok' -and $health.migration -eq 'current') { return }
    } catch {}
    Start-Sleep -Seconds 1
  }
  throw 'Multi-worker health check did not become ready'
} finally {
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
```

Expected: health becomes ready and the server process tree is stopped after the check.

- [ ] **Step 7: Commit final documentation and verification changes**

```powershell
git add README.md .env.example docs/postgresql-15-deployment.md pyproject.toml
git commit -m "docs: add postgresql 15 deployment runbook"
```

- [ ] **Step 8: Review the final commit range**

```powershell
git status --short
git log --oneline --decorate -15
git diff 8dfaf63..HEAD --stat
```

Expected: clean worktree and a focused diff from the approved design commit, with no untracked database dumps, installer files,
password files, or SQLite database files.
