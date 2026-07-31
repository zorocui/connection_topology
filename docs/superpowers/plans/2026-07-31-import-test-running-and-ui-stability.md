# Import Test Running State and UI Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Excel 导入连接测试具有等待、运行、成功、失败的真实状态，避免 Linux SSH 大输出永久阻塞，并让页面轮询保留已展开的逐行结果。

**Architecture:** 行任务在短数据库事务中从 `pending` 原子领取为 `running`，网络测试在事务外并发执行，终态保存后统一重算批次计数；启动时只恢复新版数据库中的遗留运行任务。Linux collector 直接轮询并排空 Paramiko channel，同时用单调时钟执行总超时。前端保存已展开内容状态，摘要轮询只更新展示而不丢失逐行结果。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy、SQLite、Paramiko、Jinja2、原生 JavaScript、pytest、Ruff、openpyxl

## Global Constraints

- 当前处于开发测试阶段，不兼容旧数据库、旧导入批次和旧接口响应格式。
- 不编写旧导入表数据迁移；实施完成后重建项目内开发数据库。
- SSH、WinRM 等专业术语保留英文，其他页面文案使用中文。
- 网络等待不得持有数据库会话或 `_database_gate`，保留现有连接测试并发能力。
- 密码、密钥和认证信息不得写入页面、日志、报告或测试快照。
- 只通过 `apply_patch` 修改源码和文档。

---

### Task 1: Add the running state to the new data contract

**Files:**
- Modify: `app/models.py`
- Modify: `app/schemas.py`
- Modify: `app/services/imports.py`
- Modify: `tests/test_imports.py`

**Interfaces:**
- Produces: `ImportTestStatus.RUNNING` with value `"running"`.
- Produces: `ImportBatch.test_running_rows: int` and `ImportBatchRead.test_running_rows: int`.
- Produces: report mapping `ImportTestStatus.RUNNING -> "正在测试"`.

- [ ] **Step 1: Write failing model/API/report tests**

Add assertions to `tests/test_imports.py` that a newly imported batch returns a zero running count and that a manually running row appears as “正在测试” in the generated workbook:

```python
assert batch.test_running_rows == 0

with app.state.session_factory() as session:
    row = session.get(ImportRowResult, row_id)
    row.test_status = ImportTestStatus.RUNNING
    row.test_message = "正在测试连接"
    batch = session.get(ImportBatch, batch_id)
    batch.test_pending_rows = 0
    batch.test_running_rows = 1
    session.commit()

report = load_workbook(BytesIO(build_import_report(app.state.session_factory, batch_id)))
values = list(report.active.values)
assert values[1][5:] == ("正在测试", "正在测试连接")
```

- [ ] **Step 2: Run tests and verify the contract is missing**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_imports.py -q`

Expected: FAIL because `RUNNING` and `test_running_rows` do not exist.

- [ ] **Step 3: Add the new model and schema fields**

Update `app/models.py`:

```python
class ImportTestStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class ImportBatch(Base):
    # existing fields remain unchanged
    test_pending_rows: Mapped[int] = mapped_column(Integer, default=0)
    test_running_rows: Mapped[int] = mapped_column(Integer, default=0)
    test_success_rows: Mapped[int] = mapped_column(Integer, default=0)
```

Update `ImportBatchRead` in `app/schemas.py`:

```python
test_pending_rows: int
test_running_rows: int
test_success_rows: int
```

Do not modify `app/migrations.py`; the development database will be rebuilt from metadata.

- [ ] **Step 4: Add the report status name**

Update the status map in `build_import_report`:

```python
test_names = {
    ImportTestStatus.PENDING: "待测试",
    ImportTestStatus.RUNNING: "正在测试",
    ImportTestStatus.SUCCESS: "成功",
    ImportTestStatus.FAILED: "失败",
    ImportTestStatus.NOT_APPLICABLE: "不适用",
}
```

- [ ] **Step 5: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_imports.py -q`

Expected: all tests in `tests/test_imports.py` PASS.

- [ ] **Step 6: Commit the data contract**

```powershell
git add app/models.py app/schemas.py app/services/imports.py tests/test_imports.py
git commit -m "feat: expose running import tests"
```

### Task 2: Claim tasks, count running work, and recover interrupted work

**Files:**
- Modify: `app/services/import_testing.py`
- Modify: `tests/test_import_testing.py`

**Interfaces:**
- Consumes: `ImportTestStatus.RUNNING`, `ImportBatch.test_running_rows` from Task 1.
- Produces: `_refresh_batch_counts(session: Session, batch_id: int) -> bool` that counts pending and running separately.
- Produces: `_load_target(row_id: int)` that commits `pending -> running` before returning.
- Produces: `resume_pending()` that resets stale `running` rows and resubmits all pending rows.

- [ ] **Step 1: Update fixtures and write failing state transition tests**

In `tests/test_import_testing.py`, initialize `test_running_rows=0` in batch fixtures and add tests with a blocking collector:

```python
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
```

Add a restart recovery test:

```python
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
```

Extend 150/1000-row tests with `assert batch.test_running_rows == 0`.

- [ ] **Step 2: Run the new tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -q`

Expected: new tests FAIL because a claimed row remains pending and stale running rows are not resumed.

- [ ] **Step 3: Count pending and running states**

In `_refresh_batch_counts`, add the running query and require both active counts to reach zero:

```python
batch.test_running_rows = session.scalar(
    select(func.count()).select_from(ImportRowResult).where(
        ImportRowResult.batch_id == batch_id,
        ImportRowResult.test_status == ImportTestStatus.RUNNING,
    )
) or 0

if batch.test_pending_rows + batch.test_running_rows == 0:
    batch.status = ImportBatchStatus.COMPLETED
    batch.finished_at = datetime.now(timezone.utc)
else:
    batch.status = ImportBatchStatus.TESTING
    batch.finished_at = None
```

- [ ] **Step 4: Atomically claim pending rows**

Inside `_load_target`, after validating the row is pending:

```python
row.test_status = ImportTestStatus.RUNNING
row.test_message = "正在测试连接"
session.flush()
self._refresh_batch_counts(session, row.batch_id)
session.commit()
```

Build and return `ImportTestTarget` before the session closes. If the linked device is missing, still commit the running state and return `None` as the target so `_test_target` produces an explicit failed outcome.

- [ ] **Step 5: Save results from active states**

Allow the normal running state and the pre-claim exception fallback pending state:

```python
if row is None or row.test_status not in {
    ImportTestStatus.PENDING,
    ImportTestStatus.RUNNING,
}:
    return None
row.test_status = outcome.status
row.test_message = outcome.message
```

- [ ] **Step 6: Recover stale running work at startup**

Replace the loader inside `resume_pending` with one database operation that resets running rows, refreshes affected batches, commits, and returns pending row IDs:

```python
running_rows = list(
    session.scalars(
        select(ImportRowResult).where(
            ImportRowResult.test_status == ImportTestStatus.RUNNING
        )
    ).all()
)
affected_batch_ids = {row.batch_id for row in running_rows}
for row in running_rows:
    row.test_status = ImportTestStatus.PENDING
    row.test_message = None
session.flush()
for batch_id in affected_batch_ids:
    self._refresh_batch_counts(session, batch_id)
row_ids = list(
    session.scalars(
        select(ImportRowResult.id).where(
            ImportRowResult.test_status == ImportTestStatus.PENDING
        )
    ).all()
)
session.commit()
return row_ids
```

- [ ] **Step 7: Run service tests including pressure scenarios**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -q`

Expected: all tests PASS, including 150 and 1000 row cases.

- [ ] **Step 8: Commit task state handling**

```powershell
git add app/services/import_testing.py tests/test_import_testing.py
git commit -m "fix: recover and expose active import tests"
```

### Task 3: Drain Paramiko output safely with a total timeout

**Files:**
- Modify: `app/collectors/linux.py`
- Modify: `tests/test_collectors.py`

**Interfaces:**
- Produces: `LinuxCollector._execute(client, command) -> tuple[int, str, str]` with continuous stdout/stderr draining.
- Produces: `CollectorError` code `command_timeout` after `self.timeout` seconds, with the SSH channel closed.

- [ ] **Step 1: Write a fake channel and failing large-output/timeout tests**

Add a fake Paramiko channel to `tests/test_collectors.py` whose `recv_exit_status` asserts that all output chunks were consumed first:

```python
class FakeChannel:
    def __init__(self, stdout_chunks=(), stderr_chunks=(), *, never_exit=False):
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_chunks = list(stderr_chunks)
        self.never_exit = never_exit
        self.closed = False

    def recv_ready(self):
        return bool(self.stdout_chunks)

    def recv(self, _size):
        return self.stdout_chunks.pop(0)

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv_stderr(self, _size):
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self):
        return not self.never_exit and not self.stdout_chunks and not self.stderr_chunks

    def recv_exit_status(self):
        assert not self.stdout_chunks
        assert not self.stderr_chunks
        return 0

    def close(self):
        self.closed = True
```

Use stream wrappers exposing `.channel` and a client returning them. Assert:

```python
code, output, error = collector._execute(client, "ss -H -tuna")
assert code == 0
assert output == "part-1part-2"
assert error == "warning"
```

For timeout, monkeypatch `time.monotonic` to return values past the deadline and `time.sleep` to a no-op, then assert `exc.value.code == "command_timeout"` and `channel.closed is True`.

- [ ] **Step 2: Run collector tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_collectors.py -q`

Expected: new fake-channel tests FAIL because the current implementation waits for exit status before reading and has no loop deadline.

- [ ] **Step 3: Implement channel draining and timeout**

Import `socket` and `time`. Replace `_execute` with logic equivalent to:

```python
def _execute(self, client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    channel = None
    try:
        _, stdout, _ = client.exec_command(command, timeout=self.timeout)
        channel = stdout.channel
        deadline = time.monotonic() + self.timeout
        output_chunks: list[bytes] = []
        error_chunks: list[bytes] = []
        while True:
            while channel.recv_ready():
                output_chunks.append(channel.recv(65536))
            while channel.recv_stderr_ready():
                error_chunks.append(channel.recv_stderr(65536))
            if channel.exit_status_ready():
                while channel.recv_ready():
                    output_chunks.append(channel.recv(65536))
                while channel.recv_stderr_ready():
                    error_chunks.append(channel.recv_stderr(65536))
                break
            if time.monotonic() >= deadline:
                channel.close()
                raise CollectorError("command_timeout", "远程 ss 命令执行超时")
            time.sleep(0.01)
        return (
            channel.recv_exit_status(),
            b"".join(output_chunks).decode("utf-8", errors="replace"),
            b"".join(error_chunks).decode("utf-8", errors="replace"),
        )
    except (socket.timeout, TimeoutError) as exc:
        if channel is not None:
            channel.close()
        raise CollectorError("command_timeout", "远程 ss 命令执行超时") from exc
    except paramiko.SSHException as exc:
        raise CollectorError("command_failed", "无法执行远程 ss 命令") from exc
```

Keep output reads bounded to 65536 bytes and do not call `stdout.read()` or `stderr.read()` afterward.

- [ ] **Step 4: Run collector tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_collectors.py -q`

Expected: all collector tests PASS.

- [ ] **Step 5: Commit the SSH fix**

```powershell
git add app/collectors/linux.py tests/test_collectors.py
git commit -m "fix: prevent SSH output deadlocks"
```

### Task 4: Show accurate progress and preserve expanded row results

**Files:**
- Modify: `app/templates/devices.html`
- Modify: `app/static/css/app.css`
- Create: `tests/test_import_frontend.py`

**Interfaces:**
- Consumes: `test_running_rows` and row status `running` from Tasks 1 and 2.
- Produces: `expandedImportBatchId: number | null` and `expandedImportRowsHtml: string` frontend state.
- Produces: import progress summary and row label “正在测试”.

- [ ] **Step 1: Write failing template contract tests**

Create `tests/test_import_frontend.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_import_summary_shows_active_counts_and_progress():
    template = (ROOT / "app/templates/devices.html").read_text(encoding="utf-8")
    assert "batch.test_pending_rows" in template
    assert "batch.test_running_rows" in template
    assert "等待测试" in template
    assert "正在测试" in template
    assert "导入测试进度" in template


def test_import_polling_preserves_expanded_rows():
    template = (ROOT / "app/templates/devices.html").read_text(encoding="utf-8")
    assert "expandedImportBatchId" in template
    assert "expandedImportRowsHtml" in template
    assert "expandedImportBatchId === batch.id" in template
    assert 'running: "正在测试"' in template
```

- [ ] **Step 2: Run the frontend tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_frontend.py -q`

Expected: both tests FAIL because active counts and persistent expanded state are absent.

- [ ] **Step 3: Add persistent frontend state and progress calculation**

Before `renderBatch`, declare:

```javascript
let expandedImportBatchId = null;
let expandedImportRowsHtml = "";
```

At the start of `renderBatch`, calculate:

```javascript
const completedTests = batch.test_success_rows + batch.test_failed_rows;
const testProgress = batch.imported_rows
  ? Math.round(completedTests * 100 / batch.imported_rows)
  : 100;
const preservedRows = expandedImportBatchId === batch.id
  ? expandedImportRowsHtml
  : "";
```

Render separate summary cards for `batch.test_pending_rows`, `batch.test_running_rows`, success and failure, followed by:

```html
<div class="import-progress-label">
  <span>导入测试进度</span><strong>${testProgress}%</strong>
</div>
<div class="scan-progress" aria-label="导入测试进度 ${testProgress}%">
  <i style="width:${testProgress}%"></i>
</div>
```

Render the row container with `${preservedRows}` instead of an empty body.

- [ ] **Step 4: Save loaded row HTML for subsequent polls**

Extend `testStatus` with `running: "正在测试"`. Build the row table into a local `rowsHtml`, then save and render it:

```javascript
expandedImportBatchId = batchId;
expandedImportRowsHtml = rowsHtml;
const container = document.getElementById(`import-rows-${batchId}`);
if (container) container.innerHTML = expandedImportRowsHtml;
```

The next poll may refresh the summary but must reuse the saved markup.

- [ ] **Step 5: Add compact responsive styles**

Update `app/static/css/app.css`:

```css
.import-summary { grid-template-columns: repeat(9, minmax(0, 1fr)); }
.import-progress-label { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; }
.import-progress-label strong { color: var(--text); font-family: var(--mono); }
```

Keep the existing mobile media rule that collapses `.import-summary` to one column.

- [ ] **Step 6: Run frontend and report tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_frontend.py tests/test_imports.py -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit the UI fix**

```powershell
git add app/templates/devices.html app/static/css/app.css tests/test_import_frontend.py
git commit -m "fix: keep import row details open"
```

### Task 5: Verify, rebuild the development database, and package

**Files:**
- Verify: `.env`
- Move aside: the exact project-local SQLite file resolved from `DATABASE_URL`
- Verify: all modified application and test files
- Create: timestamped `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: clean new development database, running local service, verified release archive, committed and pushed `main` only after the user-selected execution workflow permits integration.

- [ ] **Step 1: Run the complete automated checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: Ruff exits 0 and every pytest test passes.

- [ ] **Step 2: Resolve and validate the database target without printing secrets**

Read only the `DATABASE_URL` key from `.env`, falling back to `sqlite:///./connection_topology.db`. Resolve the SQLite path to an absolute path and verify all of the following before moving it:

```text
scheme == sqlite
resolved parent == C:\Users\czh\Desktop\连接拓扑图
resolved filename == connection_topology.db
```

If any check differs, stop without moving a file and report the exact non-secret path mismatch.

- [ ] **Step 3: Stop the exact local application process and move aside the old database**

Identify the process listening on the configured local service port, verify its command line belongs to this workspace, and stop only that PID. Move the validated database to:

```text
C:\Users\czh\Desktop\连接拓扑图\connection_topology.pre-import-running-state.db
```

If that backup name already exists, append a Beijing timestamp. This is a recoverable reset; do not recursively delete anything.

- [ ] **Step 4: Start the service and verify the clean schema**

Start the project with the existing startup command and a hidden window. Verify:

```text
GET http://127.0.0.1:8000/ returns HTTP 200
import_batches has test_running_rows
application log contains no startup traceback
```

- [ ] **Step 5: Re-run focused tests after the clean-database startup**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py tests/test_collectors.py tests/test_import_frontend.py tests/test_imports.py -q
```

Expected: all focused tests PASS.

- [ ] **Step 6: Build and inspect the timestamped Linux archive**

Use the repository's existing packaging script. Verify the archive excludes `.env`, `*.db`, logs, virtual environments, previous archives, `.git`, and `.superpowers`; verify it includes `requirements.txt`, application code, templates, static assets, README and deployment scripts.

- [ ] **Step 7: Confirm the implementation worktree is clean**

Run: `git status --short`

Expected: no output. If a generated database, log, archive or backup appears, update the existing ignore rules rather than committing runtime data; then rerun the complete checks before committing that ignore-rule correction.

- [ ] **Step 8: Push the completed branch after final branch workflow**

Run the finishing-development-branch workflow, then push the selected integrated branch using Git Credential Manager. Verify local HEAD equals `origin/main` when `main` is selected.
