# 批量导入数据库连接池韧性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 150 台设备的导入连接测试在 20 并发下不耗尽数据库连接池，并允许内网部署通过环境变量调整连接池。

**Architecture:** SQLAlchemy 连接池扩大为默认 20 个常驻连接、10 个溢出连接和 60 秒等待，并通过 `Settings` 注入引擎。`ImportTestService` 将数据库读取、远程 SSH/WinRM 测试、结果写回拆成三个阶段，远程等待期间没有活动数据库会话。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、Pydantic Settings 2、SQLite WAL、ThreadPoolExecutor、pytest。

## Global Constraints

- 默认 `DB_POOL_SIZE=20`、`DB_MAX_OVERFLOW=10`、`DB_POOL_TIMEOUT_SECONDS=60`。
- 配置范围分别为 1–200、0–200、1–300。
- 保持 `IMPORT_TEST_MAX_WORKERS=20`。
- 远程 SSH/WinRM 测试期间不得持有 SQLAlchemy Session 或连接。
- 网络失败可以写入逐行测试结果；数据库基础设施异常只能记录日志，不得伪装成设备连接失败。
- 不新增数据库表、字段或迁移版本。
- 继续只运行一个 Uvicorn 进程。
- 用户已明确不使用 Git；所有任务跳过分支、暂存、提交、合并和推送。

---

## 文件结构

- Modify: `app/config.py`
  - 定义连接池环境变量、默认值和范围。
- Modify: `app/database.py`
  - 将连接池配置传给 SQLAlchemy 引擎。
- Modify: `app/main.py`
  - 从 Settings 向数据库引擎传递三个参数。
- Modify: `app/services/import_testing.py`
  - 实现三阶段短会话连接测试和后台异常日志。
- Modify: `tests/test_config.py`
  - 验证默认值和配置范围。
- Create: `tests/test_database_pool.py`
  - 验证引擎采用指定连接池参数。
- Modify: `tests/test_import_testing.py`
  - 验证行为、竞态、连接释放和 150 行压力。
- Modify: `.env.example`
  - 添加三个连接池变量。
- Modify: `README.md`
  - 添加连接池调优和单进程部署说明。

---

### Task 1: 可配置数据库连接池

**Files:**
- Modify: `app/config.py`
- Modify: `app/database.py`
- Modify: `app/main.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_database_pool.py`

**Interfaces:**
- Produces: `Settings.db_pool_size: int`
- Produces: `Settings.db_max_overflow: int`
- Produces: `Settings.db_pool_timeout_seconds: int`
- Produces: `create_database_engine(database_url, sqlite_busy_timeout_ms=30000, pool_size=20, max_overflow=10, pool_timeout_seconds=60) -> Engine`

- [ ] **Step 1: 编写配置默认值和范围测试**

在 `tests/test_config.py` 的默认值测试中加入：

```python
assert settings.db_pool_size == 20
assert settings.db_max_overflow == 10
assert settings.db_pool_timeout_seconds == 60
```

新增参数化范围测试：

```python
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_pool_size", 0),
        ("db_pool_size", 201),
        ("db_max_overflow", -1),
        ("db_max_overflow", 201),
        ("db_pool_timeout_seconds", 0),
        ("db_pool_timeout_seconds", 301),
    ],
)
def test_settings_reject_invalid_database_pool_values(
    valid_key,
    field,
    value,
):
    with pytest.raises(ValidationError):
        Settings(
            app_secret_key=valid_key,
            _env_file=None,
            **{field: value},
        )
```

- [ ] **Step 2: 编写引擎池参数测试**

创建 `tests/test_database_pool.py`：

```python
from sqlalchemy import text

from app.database import create_database_engine


def test_database_engine_uses_configured_queue_pool(tmp_path):
    engine = create_database_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        sqlite_busy_timeout_ms=45000,
        pool_size=7,
        max_overflow=3,
        pool_timeout_seconds=12,
    )
    try:
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 3
        assert engine.pool._timeout == 12
        with engine.connect() as connection:
            assert connection.execute(
                text("PRAGMA busy_timeout")
            ).scalar_one() == 45000
            assert connection.execute(
                text("PRAGMA journal_mode")
            ).scalar_one().lower() == "wal"
    finally:
        engine.dispose()
```

- [ ] **Step 3: 运行新增测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_database_pool.py -q
```

Expected: FAIL，因为 Settings 字段和引擎参数尚不存在。

- [ ] **Step 4: 增加 Settings 字段**

在 `app/config.py` 的 SQLite 配置附近加入：

```python
db_pool_size: int = Field(default=20, ge=1, le=200)
db_max_overflow: int = Field(default=10, ge=0, le=200)
db_pool_timeout_seconds: int = Field(default=60, ge=1, le=300)
```

- [ ] **Step 5: 扩展数据库引擎工厂**

把 `app/database.py` 的函数签名改为：

```python
def create_database_engine(
    database_url: str,
    sqlite_busy_timeout_ms: int = 30000,
    pool_size: int = 20,
    max_overflow: int = 10,
    pool_timeout_seconds: int = 60,
) -> Engine:
```

构造引擎时显式传入：

```python
engine = create_engine(
    database_url,
    connect_args=connect_args,
    pool_size=pool_size,
    max_overflow=max_overflow,
    pool_timeout=pool_timeout_seconds,
)
```

保留全部现有 SQLite `connect` 事件和 PRAGMA。

- [ ] **Step 6: 从应用配置传入引擎**

在 `app/main.py` 中修改：

```python
engine = create_database_engine(
    resolved.database_url,
    resolved.sqlite_busy_timeout_ms,
    resolved.db_pool_size,
    resolved.db_max_overflow,
    resolved.db_pool_timeout_seconds,
)
```

- [ ] **Step 7: 运行配置和引擎测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_database_pool.py -q
.\.venv\Scripts\python.exe -m ruff check app\config.py app\database.py app\main.py tests\test_config.py tests\test_database_pool.py
```

Expected: 全部 PASS，Ruff 显示 `All checks passed!`。

---

### Task 2: 远程测试期间释放数据库连接

**Files:**
- Modify: `app/services/import_testing.py`
- Modify: `tests/test_import_testing.py`

**Interfaces:**
- Produces: `ImportTestTarget`
- Produces: `ImportTestOutcome`
- Produces: `ImportTestService._load_target(row_id) -> tuple[int, ImportTestTarget | None] | None`
- Produces: `ImportTestService._test_target(target) -> ImportTestOutcome`
- Produces: `ImportTestService._save_result(row_id, outcome) -> int | None`
- Preserves: `schedule_batch(batch_id) -> None`
- Preserves: `resume_pending() -> None`
- Preserves: `test_row(row_id) -> None`

- [ ] **Step 1: 增加测试所需的并发工具**

在 `tests/test_import_testing.py` 增加：

```python
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import text
```

定义只在测试中使用的 collector：

```python
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
```

- [ ] **Step 2: 编写网络等待时连接可用的失败测试**

增加批量种子辅助函数：

```python
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
```

新增测试：

```python
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
        assert batch.test_success_rows == count
```

为了真正限制测试池，将 `tests/conftest.py` 的测试 Settings 显式设置：

```python
db_pool_size=2,
db_max_overflow=0,
db_pool_timeout_seconds=1,
```

- [ ] **Step 3: 编写竞态行为测试**

增加：

```python
def test_completed_row_is_not_overwritten(app):
    batch_id, row_id, _ = seed_pending_row(app, "10.0.0.31")
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
```

- [ ] **Step 4: 运行新测试并确认旧实现失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_import_testing.py -q
```

Expected: 连接释放测试 FAIL，旧实现的 20 个线程无法同时越过容量为 2 的连接池。

- [ ] **Step 5: 定义不可变目标和结果**

在 `app/services/import_testing.py` 加入：

```python
import logging
from dataclasses import dataclass

from app.collectors.base import Collector, DeviceConnectionSpec
from app.models import OSType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportTestTarget:
    os_type: OSType
    host: str
    port: int
    username: str
    encrypted_password: str


@dataclass(frozen=True)
class ImportTestOutcome:
    status: ImportTestStatus
    message: str
```

- [ ] **Step 6: 实现阶段一目标读取**

在 `ImportTestService` 中加入：

```python
def _load_target(
    self,
    row_id: int,
) -> tuple[int, ImportTestTarget | None] | None:
    with self.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        if row is None or row.test_status != ImportTestStatus.PENDING:
            return None
        batch_id = row.batch_id
        device = session.get(Device, row.device_id)
        if device is None:
            return batch_id, None
        return batch_id, ImportTestTarget(
            os_type=device.os_type,
            host=device.host,
            port=device.port,
            username=device.username,
            encrypted_password=device.encrypted_password,
        )
```

- [ ] **Step 7: 实现无会话网络测试**

加入：

```python
def _test_target(
    self,
    target: ImportTestTarget | None,
) -> ImportTestOutcome:
    if target is None:
        return ImportTestOutcome(
            ImportTestStatus.FAILED,
            "导入设备不存在",
        )
    password = ""
    try:
        password = self.cipher.decrypt(target.encrypted_password)
        collector = (
            self.linux_collector
            if target.os_type == OSType.LINUX
            else self.windows_collector
        )
        collector.test_connection(
            DeviceConnectionSpec(
                host=target.host,
                port=target.port,
                username=target.username,
            ),
            password,
        )
        return ImportTestOutcome(
            ImportTestStatus.SUCCESS,
            "连接测试成功",
        )
    except Exception as exc:  # noqa: BLE001
        return ImportTestOutcome(
            ImportTestStatus.FAILED,
            safe_error_message(str(exc), (password,)),
        )
```

- [ ] **Step 8: 实现阶段三写回**

加入：

```python
def _save_result(
    self,
    row_id: int,
    outcome: ImportTestOutcome,
) -> int | None:
    completed_batch_id: int | None = None
    with self.session_factory() as session:
        row = session.get(ImportRowResult, row_id)
        if row is None or row.test_status != ImportTestStatus.PENDING:
            return None
        row.test_status = outcome.status
        row.test_message = outcome.message
        session.flush()
        if self._refresh_batch_counts(session, row.batch_id):
            completed_batch_id = row.batch_id
        session.commit()
    return completed_batch_id
```

把 `test_row()` 替换为：

```python
def test_row(self, row_id: int) -> None:
    loaded = self._load_target(row_id)
    if loaded is None:
        return
    _, target = loaded
    outcome = self._test_target(target)
    completed_batch_id = self._save_result(row_id, outcome)
    if completed_batch_id is not None and self.batch_completed_callback:
        self.batch_completed_callback(completed_batch_id)
```

- [ ] **Step 9: 记录后台数据库异常**

增加统一提交方法：

```python
def _future_done(self, future) -> None:
    exception = future.exception()
    if exception is not None:
        logger.error(
            "导入连接测试后台任务异常",
            exc_info=(
                type(exception),
                exception,
                exception.__traceback__,
            ),
        )


def _submit(self, row_id: int) -> None:
    future = self.executor.submit(self.test_row, row_id)
    if future is not None and hasattr(future, "add_done_callback"):
        future.add_done_callback(self._future_done)
```

将 `schedule_batch()` 和 `resume_pending()` 中的：

```python
self.executor.submit(self.test_row, row_id)
```

替换为：

```python
self._submit(row_id)
```

- [ ] **Step 10: 验证完成回调与后台异常日志**

在 `tests/test_import_testing.py` 增加：

```python
from concurrent.futures import Future


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
```

- [ ] **Step 11: 运行导入测试和静态检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_import_testing.py tests\test_imports.py -q
.\.venv\Scripts\python.exe -m ruff check app\services\import_testing.py tests\test_import_testing.py tests\conftest.py
```

Expected: 全部 PASS，Ruff 显示 `All checks passed!`。

---

### Task 3: 150 台压力回归、文档、正式验收与打包

**Files:**
- Modify: `tests/test_import_testing.py`
- Modify: `.env.example`
- Modify: `README.md`
- Verify: complete project
- Produce: `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

**Interfaces:**
- Consumes: 可配置连接池和三阶段导入测试。
- Produces: 150 台压力测试、部署说明、正式服务和 Linux 包。

- [ ] **Step 1: 增加 150 台压力测试**

在 `tests/test_import_testing.py` 增加：

```python
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
    )
    try:
        service.schedule_batch(batch_id)
    finally:
        executor.shutdown(wait=True)

    with app.state.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.test_pending_rows == 0
        assert batch.test_success_rows == count
        assert batch.test_failed_rows == 0
```

- [ ] **Step 2: 运行压力测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_import_testing.py::test_150_import_tests_complete_without_pool_timeout -q
```

Expected: PASS，且输出中没有 `QueuePool limit`。

- [ ] **Step 3: 更新环境变量示例**

在 `.env.example` 的数据库配置后加入：

```env
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=10
DB_POOL_TIMEOUT_SECONDS=60
```

- [ ] **Step 4: 更新 README**

在并发环境变量段加入：

```markdown
- `DB_POOL_SIZE`：数据库常驻连接数，默认 20。
- `DB_MAX_OVERFLOW`：连接池繁忙时允许的临时额外连接数，默认 10。
- `DB_POOL_TIMEOUT_SECONDS`：获取数据库连接的最长等待秒数，默认 60。

导入连接测试读取设备参数后会释放数据库会话，SSH/WinRM 网络等待不会占用
连接池。默认 20 + 10 的连接容量用于页面请求和短时结果写入。SQLite 部署仍
必须只运行一个 Uvicorn 进程；增加连接池不代表可以启用多个 Web workers。
```

- [ ] **Step 5: 运行完整验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts
node --check app\static\js\scan-batches.js
node --check app\static\js\device-list.js
node --check app\static\js\topology.js
```

Expected: 全部测试 PASS，Ruff 显示 `All checks passed!`，Node 命令退出码为 0。

- [ ] **Step 6: 重启正式服务并检查连接池**

重启 `http://127.0.0.1:8000` 的单进程 Uvicorn 服务。通过应用状态或一个只读本地诊断脚本确认：

```python
assert app.state.engine.pool.size() == 20
assert app.state.engine.pool._max_overflow == 10
assert app.state.engine.pool._timeout == 60
```

确认设备管理页和导入接口可访问，浏览器控制台没有新增 error 或 warning。

- [ ] **Step 7: 生成带时间戳 Linux 包**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
```

Expected: 生成新的 `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`。

- [ ] **Step 8: 检查压缩包**

使用 `tar -tzf` 验证：

- 包含更新后的 `app/config.py`、`app/database.py`、`app/main.py`、`app/services/import_testing.py`、`.env.example` 和 README。
- 不包含 `.git`、`.venv`、`tests`、缓存或数据库文件。
- 报告 `.env` 和 `wheelhouse` 是否包含。

- [ ] **Step 9: 最终交付**

报告：

- 根因和双重修复方式。
- 150 台压力测试结果。
- 完整测试数量与静态检查结果。
- 正式服务状态。
- 新 Linux 包路径。
- `.env`、`wheelhouse` 和 Git 状态。
