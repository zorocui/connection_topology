# Import Test Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保证 Excel 批量导入的每条连接测试可靠进入终态，并在批次结束后自动创建首次扫描批次。

**Architecture:** `ImportTestService` 保留现有网络线程池并发，在数据库会话外执行 SSH/WinRM 等待；所有短数据库阶段通过服务级互斥门执行，并对连接池超时和 SQLite 忙/锁定进行有限重试。未处理的测试阶段异常转为逐行失败结果，使批次计数和完成回调能够继续推进。

**Tech Stack:** Python 3.10、SQLAlchemy 2、SQLite WAL、concurrent.futures、pytest

## Global Constraints

- 网络连接测试继续使用现有 `IMPORT_TEST_MAX_WORKERS` 并发数。
- 数据库阶段最多重试 3 次，重试等待依次为 100 毫秒、300 毫秒。
- 不修改 Excel 格式、1000 行上限、设备去重规则或 API 格式。
- 失败原因必须经过 `safe_error_message` 脱敏。
- 第一阶段不改页面布局或导入交互。
- 用户已明确要求在当前会话连续完成，不再请求执行方式选择；在当前 `main` 分支实施、验证、提交并推送。

---

### Task 1: Add a database gate and transient retry

**Files:**
- Modify: `app/services/import_testing.py`
- Modify: `tests/test_import_testing.py`

**Interfaces:**
- Consumes: `sessionmaker[Session]`、SQLAlchemy `TimeoutError` 和 `OperationalError`
- Produces: `ImportTestService._run_database_operation(operation: Callable[[], T]) -> T`

- [ ] **Step 1: Write a failing retry test**

在 `tests/test_import_testing.py` 中创建一个操作：前两次抛出 SQLAlchemy `TimeoutError`，第三次返回 `"ok"`。把 `time.sleep` 替换为空操作，断言 `_run_database_operation` 返回 `"ok"`、操作执行 3 次、等待序列为 `[0.1, 0.3]`。

- [ ] **Step 2: Run the focused retry test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -k database_operation_retries -q`

Expected: FAIL，因为 `_run_database_operation` 尚不存在。

- [ ] **Step 3: Implement the database operation wrapper**

在 `app/services/import_testing.py`：

```python
import threading
import time
from typing import TypeVar

from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError

T = TypeVar("T")
DATABASE_RETRY_DELAYS = (0.1, 0.3)
```

在 `ImportTestService.__init__` 创建 `self._database_gate = threading.Lock()`。

新增：

```python
def _is_transient_database_error(exc: Exception) -> bool:
    if isinstance(exc, SATimeoutError):
        return True
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message

def _run_database_operation(self, operation: Callable[[], T]) -> T:
    for attempt in range(len(DATABASE_RETRY_DELAYS) + 1):
        try:
            with self._database_gate:
                return operation()
        except Exception as exc:
            if (
                not _is_transient_database_error(exc)
                or attempt == len(DATABASE_RETRY_DELAYS)
            ):
                raise
            time.sleep(DATABASE_RETRY_DELAYS[attempt])
    raise AssertionError("数据库重试循环未返回")
```

- [ ] **Step 4: Route short database phases through the wrapper**

把待测试行查询、`_load_target` 和 `_save_result` 的原有会话代码分别提取为单次操作，并通过 `_run_database_operation` 调用。睡眠必须发生在互斥门之外，每次重试必须重新创建会话。

- [ ] **Step 5: Run retry and network concurrency tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -k "database_operation_retries or network_wait" -q`

Expected: PASS，且 20 个网络测试仍能同时进入 collector。

### Task 2: Terminalize unexpected row failures

**Files:**
- Modify: `app/services/import_testing.py`
- Modify: `tests/test_import_testing.py`

**Interfaces:**
- Consumes: `_load_target`、`_test_target`、`_save_result`
- Produces: `ImportTestService.test_row(row_id: int) -> None` 的异常终结保证

- [ ] **Step 1: Write a failing unexpected-error test**

创建一条待测试记录，把该服务实例的 `_load_target` 替换为抛出 `RuntimeError("database unavailable")` 的函数，执行 `test_row`。断言该行变为 `ImportTestStatus.FAILED`，错误消息含“连接测试内部异常”，批次变为 `COMPLETED` 且失败数为 1。

- [ ] **Step 2: Run the focused failure test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -k unexpected_row_error -q`

Expected: FAIL，因为异常会直接逃逸且记录保持 `pending`。

- [ ] **Step 3: Convert unexpected work errors into a persisted outcome**

将 `test_row` 组织为：

```python
try:
    loaded = self._load_target(row_id)
    if loaded is None:
        return
    _, target = loaded
    outcome = self._test_target(target)
except Exception as exc:
    logger.exception("导入连接测试任务发生内部异常，行记录 %s", row_id)
    outcome = ImportTestOutcome(
        ImportTestStatus.FAILED,
        f"连接测试内部异常：{safe_error_message(str(exc), ())}",
    )

completed_batch_id = self._save_result(row_id, outcome)
if completed_batch_id is not None and self.batch_completed_callback:
    self.batch_completed_callback(completed_batch_id)
```

结果保存本身仍由 Task 1 的数据库重试保护；如果数据库持续不可用，Future 日志和启动时 `resume_pending` 继续提供恢复路径。

- [ ] **Step 4: Run import service behavior tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -k "unexpected_row_error or background_import or callback_runs_once or future_database_exception" -q`

Expected: PASS。

### Task 3: Prove 150/1000-row completion and first scan creation

**Files:**
- Modify: `tests/test_import_testing.py`
- Verify: `app/services/import_testing.py`
- Verify: `app/services/scan_queue.py`

**Interfaces:**
- Consumes: `ImportTestService.schedule_batch(batch_id)` 和 `ScanQueueService.create_import_scan_batch(import_batch_id)`
- Produces: 大批量导入不会残留 `pending` 且生成唯一首次扫描批次的回归保证

- [ ] **Step 1: Strengthen the existing 150-row pressure test**

给现有 `test_150_import_tests_complete_without_pool_timeout` 传入 `app.state.scan_queue.create_import_scan_batch` 完成回调，并断言：

```python
assert batch.status == ImportBatchStatus.COMPLETED
assert batch.test_pending_rows == 0
assert batch.test_success_rows == 150
assert batch.scan_batch_id is not None
scan_batch = session.get(ScanBatch, batch.scan_batch_id)
assert scan_batch.total_tasks == 150
```

- [ ] **Step 2: Add a 1000-row pressure test**

使用相同的 20 线程执行器、测试数据库池大小 2、无 overflow、1 秒超时，创建 1000 条待测试记录。断言 1000 条全部成功、无 `pending`、导入批次完成、首次扫描批次包含 1000 个任务且只关联一个扫描批次。

- [ ] **Step 3: Run both pressure tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -k "150_import or 1000_import" -q`

Expected: 2 passed。

- [ ] **Step 4: Run the complete import test module**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_import_testing.py -q`

Expected: 全部通过。

### Task 4: Full validation, runtime restart, commit, and push

**Files:**
- Verify: `app/services/import_testing.py`
- Verify: `tests/test_import_testing.py`
- Update if needed: `README.md`

**Interfaces:**
- Consumes: 完成的可靠性实现
- Produces: 通过验证并部署到本地运行服务和 GitHub `main`

- [ ] **Step 1: Run static checks**

Run: `.\.venv\Scripts\python.exe -m ruff check app tests`

Expected: `All checks passed!`

- [ ] **Step 2: Run the full test suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: 全部测试通过，包括此前失败的 150 行压力测试。

- [ ] **Step 3: Restart and smoke-test the local service**

只终止命令行包含 `uvicorn app.main:app --host 127.0.0.1 --port 8000` 的 8000 端口进程，使用项目 `.venv` 隐藏窗口重启。验证 `GET http://127.0.0.1:8000/devices` 返回 200，错误日志没有新的导入任务异常。

- [ ] **Step 4: Commit and push**

```powershell
git add app/services/import_testing.py tests/test_import_testing.py docs/superpowers/plans/2026-07-31-import-test-reliability.md
git commit -m "fix: make bulk import testing reliable"
git push origin main
```

Expected: 本地 `main` 与 `origin/main` 指向同一提交，工作区干净。
