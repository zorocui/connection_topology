# 批量采集失败明细与失败重试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设备管理页实时、分页展示批量采集失败设备及原因，并能将失败设备创建为新的重试批次。

**Architecture:** 复用 `ScanBatchItem`、`ScanTask`、`Device` 和 `Cluster`，由独立查询服务完成批次内失败成员的分页、搜索和稳定排序。FastAPI 提供失败明细与失败重试接口；前端使用单个独立面板，仅在面板打开且批次未完成时轮询。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、Pydantic 2、SQLite、Jinja2、原生 JavaScript/CSS、pytest。

## Global Constraints

- 不新增数据库表或字段；仅向 Python 枚举增加 `ScanBatchType.RETRY = "retry"`。
- 失败明细包含 `FAILED` 和 `CANCELLED`，与现有 `failed_tasks` 汇总口径一致。
- 每页默认 20 条，页面可选 10、20、50 条，接口最大允许 100 条。
- 时间统一以 `Asia/Shanghai` 显示。
- 不导出 Excel，不引入 WebSocket 或 SSE，不向页面暴露异常堆栈。
- 重试创建新批次，不修改原批次。
- 用户已明确不使用 Git；所有任务跳过分支、暂存、提交、合并和推送。

---

## 文件结构

- Create: `app/services/scan_batch_failures.py`
  - 只负责失败成员查询、分页、搜索和可重试设备 ID 提取。
- Create: `app/static/js/scan-batches.js`
  - 只负责批次卡片、失败明细面板、轮询和失败重试交互。
- Create: `tests/test_scan_batch_failures.py`
  - 服务层分页、搜索、隔离、默认错误信息测试。
- Create: `tests/test_scan_batch_frontend.py`
  - 模板与 JavaScript 交互契约测试。
- Modify: `app/models.py`
  - 新增 `ScanBatchType.RETRY`。
- Modify: `app/schemas.py`
  - 新增失败明细响应模型。
- Modify: `app/routes/api.py`
  - 新增失败明细和失败重试接口。
- Modify: `app/templates/devices.html`
  - 增加独立失败明细面板，改为加载专用批次脚本。
- Modify: `app/static/css/app.css`
  - 增加明细面板、工具栏、状态和响应式样式。
- Modify: `tests/test_scan_queue_api.py`
  - 新增接口和失败重试集成测试。
- Modify: `README.md`
  - 说明失败定位和失败重试能力。

---

### Task 1: 失败明细查询服务与响应模型

**Files:**
- Create: `app/services/scan_batch_failures.py`
- Modify: `app/schemas.py`
- Test: `tests/test_scan_batch_failures.py`

**Interfaces:**
- Produces: `FAILED_BATCH_ITEM_STATUSES: tuple[ScanTaskStatus, ...]`
- Produces: `BatchFailureRow`、`BatchFailurePage`
- Produces: `list_batch_failures(session, batch_id, page, page_size, query) -> BatchFailurePage`
- Produces: `failed_device_ids(session, batch_id) -> list[int]`
- Produces: `ScanBatchFailureItemRead`、`ScanBatchFailurePageRead`

- [ ] **Step 1: 编写分页、搜索和默认错误信息的失败测试**

在 `tests/test_scan_batch_failures.py` 中创建辅助函数，直接写入设备、集群、批次、任务和成员：

```python
from datetime import datetime, timezone

from app.models import (
    Cluster,
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
from app.services.scan_batch_failures import (
    failed_device_ids,
    list_batch_failures,
)


NOW = datetime(2026, 7, 31, 4, 20, tzinfo=timezone.utc)


def add_batch_item(session, cipher, batch, *, index, status, error, cluster=None):
    device = Device(
        name=f"节点-{index}",
        host=f"10.20.0.{index}",
        os_type=OSType.LINUX,
        port=22,
        username="ops",
        encrypted_password=cipher.encrypt("secret"),
        cluster=cluster,
    )
    task = ScanTask(
        device=device,
        trigger_type=ScanTrigger.BATCH,
        priority=20,
        status=status,
        error_message=error,
        started_at=NOW,
        finished_at=NOW,
    )
    session.add_all([device, task])
    session.flush()
    session.add(
        ScanBatchItem(
            batch=batch,
            task=task,
            device_id=device.id,
            status=status,
        )
    )
    return device
```

```python
def test_lists_only_failed_items_with_pagination_and_stable_order(app):
    with app.state.session_factory() as session:
        batch = ScanBatch(
            batch_type=ScanBatchType.ALL,
            status=ScanBatchStatus.COMPLETED,
        )
        session.add(batch)
        session.flush()
        for index, status in enumerate(
            [
                ScanTaskStatus.FAILED,
                ScanTaskStatus.FAILED,
                ScanTaskStatus.FAILED,
                ScanTaskStatus.CANCELLED,
                ScanTaskStatus.SUCCESS,
            ],
            start=1,
        ):
            add_batch_item(
                session,
                app.state.cipher,
                batch,
                index=index,
                status=status,
                error=f"错误-{index}",
            )
        session.commit()
        batch_id = batch.id

    with app.state.session_factory() as session:
        first = list_batch_failures(session, batch_id, 1, 2, "")
        second = list_batch_failures(session, batch_id, 2, 2, "")

    assert first.total == 4
    assert first.pages == 2
    assert len(first.items) == 2
    assert len(second.items) == 2
    assert {item.device_id for item in first.items}.isdisjoint(
        item.device_id for item in second.items
    )
    assert all(
        item.status in {ScanTaskStatus.FAILED, ScanTaskStatus.CANCELLED}
        for item in [*first.items, *second.items]
    )


def test_searches_name_host_cluster_and_error(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="核心集群")
        batch = ScanBatch(
            batch_type=ScanBatchType.CLUSTER,
            status=ScanBatchStatus.COMPLETED,
        )
        session.add_all([cluster, batch])
        session.flush()
        device = add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=8,
            status=ScanTaskStatus.FAILED,
            error="SSH 认证失败",
            cluster=cluster,
        )
        session.commit()
        batch_id, device_id = batch.id, device.id

    with app.state.session_factory() as session:
        for query in ("节点-8", "10.20.0.8", "核心集群", "认证失败"):
            page = list_batch_failures(session, batch_id, 1, 20, query)
            assert [item.device_id for item in page.items] == [device_id]


def test_failure_message_has_safe_fallback(app):
    with app.state.session_factory() as session:
        batch = ScanBatch(
            batch_type=ScanBatchType.ALL,
            status=ScanBatchStatus.COMPLETED,
        )
        session.add(batch)
        session.flush()
        add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=1,
            status=ScanTaskStatus.FAILED,
            error=None,
        )
        add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=2,
            status=ScanTaskStatus.CANCELLED,
            error=None,
        )
        session.commit()
        batch_id = batch.id

    with app.state.session_factory() as session:
        page = list_batch_failures(session, batch_id, 1, 20, "")

    messages = {item.status: item.error_message for item in page.items}
    assert messages[ScanTaskStatus.FAILED] == "采集任务发生内部错误"
    assert messages[ScanTaskStatus.CANCELLED] == "采集任务已取消"


def test_failed_device_ids_are_unique_and_sorted(app):
    with app.state.session_factory() as session:
        batch = ScanBatch(
            batch_type=ScanBatchType.ALL,
            status=ScanBatchStatus.COMPLETED,
        )
        session.add(batch)
        session.flush()
        second = add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=2,
            status=ScanTaskStatus.CANCELLED,
            error="已取消",
        )
        first = add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=1,
            status=ScanTaskStatus.FAILED,
            error="连接超时",
        )
        add_batch_item(
            session,
            app.state.cipher,
            batch,
            index=3,
            status=ScanTaskStatus.SUCCESS,
            error=None,
        )
        session.commit()
        batch_id = batch.id
        expected = sorted([first.id, second.id])

    with app.state.session_factory() as session:
        assert failed_device_ids(session, batch_id) == expected
```

- [ ] **Step 2: 运行新增测试并确认按预期失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_batch_failures.py -q
```

Expected: FAIL，原因是 `app.services.scan_batch_failures` 尚不存在。

- [ ] **Step 3: 新增失败明细响应模型**

在 `app/schemas.py` 中加入：

```python
class ScanBatchFailureItemRead(BaseModel):
    device_id: int
    device_name: str
    host: str
    cluster_name: str | None
    status: ScanTaskStatus
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None


class ScanBatchFailurePageRead(BaseModel):
    batch_id: int
    batch_status: ScanBatchStatus
    total: int
    page: int
    page_size: int
    pages: int
    items: list[ScanBatchFailureItemRead]
```

- [ ] **Step 4: 实现专用失败查询服务**

创建 `app/services/scan_batch_failures.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Cluster,
    Device,
    ScanBatch,
    ScanBatchItem,
    ScanBatchStatus,
    ScanTask,
    ScanTaskStatus,
)


FAILED_BATCH_ITEM_STATUSES = (
    ScanTaskStatus.FAILED,
    ScanTaskStatus.CANCELLED,
)


@dataclass(frozen=True)
class BatchFailureRow:
    device_id: int
    device_name: str
    host: str
    cluster_name: str | None
    status: ScanTaskStatus
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class BatchFailurePage:
    batch_id: int
    batch_status: ScanBatchStatus
    total: int
    page: int
    page_size: int
    pages: int
    items: list[BatchFailureRow]
```

实现共享过滤条件，确保计数查询和数据查询完全一致：

```python
def _failure_filters(batch_id: int, query: str):
    filters = [
        ScanBatchItem.batch_id == batch_id,
        ScanBatchItem.status.in_(FAILED_BATCH_ITEM_STATUSES),
    ]
    normalized = query.strip()
    if normalized:
        pattern = f"%{normalized}%"
        filters.append(
            or_(
                Device.name.ilike(pattern),
                Device.host.ilike(pattern),
                Cluster.name.ilike(pattern),
                ScanTask.error_message.ilike(pattern),
            )
        )
    return filters
```

`list_batch_failures()` 必须：

1. 使用 `session.get(ScanBatch, batch_id)`；不存在时抛出 `LookupError("扫描批次不存在")`。
2. 连接 `ScanBatchItem.task` 和 `ScanTask.device`，外连接 `Device.cluster`。
3. 先执行 `count(*)`。
4. 使用 `ScanTask.finished_at.desc().nullslast(), ScanBatchItem.id.desc()` 稳定排序。
5. 使用 `.offset((page - 1) * page_size).limit(page_size)`。
6. 将空错误信息映射为安全中文默认值。
7. 返回 `BatchFailurePage`。

实现可重试设备查询：

```python
def failed_device_ids(session: Session, batch_id: int) -> list[int]:
    return list(
        session.scalars(
            select(ScanBatchItem.device_id)
            .where(
                ScanBatchItem.batch_id == batch_id,
                ScanBatchItem.status.in_(FAILED_BATCH_ITEM_STATUSES),
            )
            .distinct()
            .order_by(ScanBatchItem.device_id)
        )
    )
```

- [ ] **Step 5: 运行服务测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_batch_failures.py -q
```

Expected: PASS。

- [ ] **Step 6: 运行静态检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app\services\scan_batch_failures.py app\schemas.py tests\test_scan_batch_failures.py
```

Expected: `All checks passed!`

---

### Task 2: 失败明细与失败重试 API

**Files:**
- Modify: `app/models.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_scan_queue_api.py`

**Interfaces:**
- Consumes: `list_batch_failures(...) -> BatchFailurePage`
- Consumes: `failed_device_ids(session, batch_id) -> list[int]`
- Produces: `GET /api/scan-batches/{batch_id}/failures`
- Produces: `POST /api/scan-batches/{batch_id}/retry-failures`
- Produces: `ScanBatchType.RETRY`

- [ ] **Step 1: 编写失败明细接口测试**

在 `tests/test_scan_queue_api.py` 中增加使用数据库直接构造失败批次的辅助函数，并验证：

```python
from datetime import datetime, timezone

from sqlalchemy import func, select

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


def seed_api_batch(app, statuses):
    with app.state.session_factory() as session:
        batch = ScanBatch(
            batch_type=ScanBatchType.ALL,
            status=ScanBatchStatus.COMPLETED,
        )
        session.add(batch)
        session.flush()
        device_ids = []
        for index, (task_status, error) in enumerate(statuses, start=1):
            device = Device(
                name=f"接口节点-{index}",
                host=f"10.30.0.{index}",
                os_type=OSType.LINUX,
                port=22,
                username="ops",
                encrypted_password=app.state.cipher.encrypt("secret"),
            )
            task = ScanTask(
                device=device,
                trigger_type=ScanTrigger.BATCH,
                priority=20,
                status=task_status,
                error_message=error,
                started_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
                finished_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
            )
            session.add_all([device, task])
            session.flush()
            session.add(
                ScanBatchItem(
                    batch=batch,
                    task=task,
                    device_id=device.id,
                    status=task_status,
                )
            )
            device_ids.append(device.id)
        batch.total_tasks = len(statuses)
        batch.success_tasks = sum(
            status == ScanTaskStatus.SUCCESS for status, _ in statuses
        )
        batch.failed_tasks = sum(
            status in {ScanTaskStatus.FAILED, ScanTaskStatus.CANCELLED}
            for status, _ in statuses
        )
        session.commit()
        return batch.id, device_ids


def test_batch_failures_endpoint_supports_pagination_and_search(client, app):
    batch_id, _ = seed_api_batch(
        app,
        [
            (ScanTaskStatus.FAILED, "SSH 认证失败"),
            (ScanTaskStatus.FAILED, "连接超时"),
            (ScanTaskStatus.SUCCESS, None),
        ],
    )
    response = client.get(
        f"/api/scan-batches/{batch_id}/failures",
        params={"page": 1, "page_size": 20, "q": "认证"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["batch_id"] == batch_id
    assert payload["total"] == 1
    assert payload["items"][0]["error_message"] == "SSH 认证失败"


def test_batch_failures_endpoint_validates_batch_and_page(client):
    assert client.get("/api/scan-batches/999999/failures").status_code == 404
    assert client.get(
        "/api/scan-batches/999999/failures?page=0"
    ).status_code == 422
    assert client.get(
        "/api/scan-batches/999999/failures?page_size=101"
    ).status_code == 422
```

- [ ] **Step 2: 编写失败重试接口测试**

覆盖新批次、原批次不变和无失败设备：

```python
def test_retry_failures_creates_new_retry_batch(client, app):
    source_id, device_ids = seed_api_batch(
        app,
        [
            (ScanTaskStatus.FAILED, "连接超时"),
            (ScanTaskStatus.CANCELLED, "任务已取消"),
            (ScanTaskStatus.SUCCESS, None),
        ],
    )
    response = client.post(
        f"/api/scan-batches/{source_id}/retry-failures"
    )
    assert response.status_code == 201
    retry = response.json()
    assert retry["id"] != source_id
    assert retry["batch_type"] == "retry"
    assert retry["total_tasks"] == 2
    with app.state.session_factory() as session:
        source = session.get(ScanBatch, source_id)
        retry_device_ids = session.scalars(
            select(ScanBatchItem.device_id)
            .where(ScanBatchItem.batch_id == retry["id"])
            .order_by(ScanBatchItem.device_id)
        ).all()
        assert source.failed_tasks == 2
        assert retry_device_ids == sorted(device_ids[:2])


def test_retry_failures_reuses_active_task(client, app):
    source_id, device_ids = seed_api_batch(
        app,
        [(ScanTaskStatus.FAILED, "连接超时")],
    )
    with app.state.session_factory() as session:
        active = ScanTask(
            device_id=device_ids[0],
            trigger_type=ScanTrigger.MANUAL,
            priority=100,
            status=ScanTaskStatus.PENDING,
        )
        session.add(active)
        session.commit()
        active_id = active.id

    response = client.post(
        f"/api/scan-batches/{source_id}/retry-failures"
    )
    assert response.status_code == 201
    with app.state.session_factory() as session:
        active_count = session.scalar(
            select(func.count())
            .select_from(ScanTask)
            .where(
                ScanTask.device_id == device_ids[0],
                ScanTask.status.in_(
                    [ScanTaskStatus.PENDING, ScanTaskStatus.RUNNING]
                ),
            )
        )
        item = session.scalar(
            select(ScanBatchItem).where(
                ScanBatchItem.batch_id == response.json()["id"]
            )
        )
        assert active_count == 1
        assert item.task_id == active_id


def test_retry_failures_rejects_batch_without_failures(client, app):
    source_id, _ = seed_api_batch(
        app,
        [(ScanTaskStatus.SUCCESS, None)],
    )
    response = client.post(
        f"/api/scan-batches/{source_id}/retry-failures"
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "该批次当前没有失败设备"
```

- [ ] **Step 3: 运行接口测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_queue_api.py -q
```

Expected: 新测试 FAIL，原因是两个接口和 `retry` 批次类型尚不存在。

- [ ] **Step 4: 新增重试批次类型**

在 `app/models.py` 中修改：

```python
class ScanBatchType(str, enum.Enum):
    ALL = "all"
    CLUSTER = "cluster"
    IMPORT = "import"
    RETRY = "retry"
```

现有 `_batch_trigger_and_priority()` 对非导入批次统一返回 `ScanTrigger.BATCH` 和批次优先级，因此无需增加新的触发类型或调度分支。

- [ ] **Step 5: 实现失败明细接口**

在 `app/routes/api.py` 导入：

```python
from app.schemas import ScanBatchFailurePageRead
from app.services.scan_batch_failures import (
    failed_device_ids,
    list_batch_failures,
)
```

新增路由：

```python
@router.get(
    "/scan-batches/{batch_id}/failures",
    response_model=ScanBatchFailurePageRead,
)
def get_scan_batch_failures(
    batch_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str = Query(default="", max_length=200),
    db: Session = Depends(get_db),
):
    try:
        return list_batch_failures(db, batch_id, page, page_size, q)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
```

- [ ] **Step 6: 实现失败重试接口**

新增路由，并确保 404 检查发生在失败 ID 查询前：

```python
@router.post(
    "/scan-batches/{batch_id}/retry-failures",
    response_model=ScanBatchRead,
    status_code=status.HTTP_201_CREATED,
)
def retry_scan_batch_failures(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    if db.get(ScanBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="扫描批次不存在")
    device_ids = failed_device_ids(db, batch_id)
    if not device_ids:
        raise HTTPException(status_code=409, detail="该批次当前没有失败设备")
    try:
        return request.app.state.scan_queue.create_batch(
            ScanBatchType.RETRY,
            device_ids,
        )
    except ScanQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
```

- [ ] **Step 7: 运行后端相关测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_batch_failures.py tests\test_scan_queue_api.py tests\test_scan_queue.py -q
```

Expected: PASS。

- [ ] **Step 8: 运行静态检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app\models.py app\schemas.py app\routes\api.py app\services\scan_batch_failures.py tests\test_scan_batch_failures.py tests\test_scan_queue_api.py
```

Expected: `All checks passed!`

---

### Task 3: 独立失败明细面板

**Files:**
- Create: `app/static/js/scan-batches.js`
- Modify: `app/templates/devices.html`
- Modify: `app/static/css/app.css`
- Create: `tests/test_scan_batch_frontend.py`

**Interfaces:**
- Consumes: `GET /api/scan-batches/{batch_id}/failures`
- Consumes: `POST /api/scan-batches/{batch_id}/retry-failures`
- Produces: `window.renderImportScanBatch(importBatchId, scanBatchId)`
- Produces: DOM IDs `scan-failure-panel`、`scan-failure-search`、`scan-failure-body`、`retry-failed-devices`

- [ ] **Step 1: 编写模板和脚本契约测试**

创建 `tests/test_scan_batch_frontend.py`：

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_devices_page_has_failure_detail_panel():
    template = (ROOT / "app/templates/devices.html").read_text(encoding="utf-8")
    assert 'id="scan-failure-panel"' in template
    assert 'id="scan-failure-search"' in template
    assert 'id="scan-failure-page-size"' in template
    assert 'id="retry-failed-devices"' in template
    assert "static/js/scan-batches.js" in template


def test_batch_script_supports_failures_polling_and_retry():
    script = (ROOT / "app/static/js/scan-batches.js").read_text(
        encoding="utf-8"
    )
    assert "/failures?${params.toString()}" in script
    assert "/retry-failures" in script
    assert "failureState.timer" in script
    assert "clearTimeout(failureState.timer)" in script
    assert 'timeZone: "Asia/Shanghai"' in script
    assert "escapeHtml(item.error_message)" in script
    assert "retryButton.disabled = true" in script
```

- [ ] **Step 2: 运行前端契约测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_batch_frontend.py -q
```

Expected: FAIL，原因是面板和脚本不存在。

- [ ] **Step 3: 在设备模板中加入失败明细面板**

在 `scan-batch-list` 后、批量扫描 section 结束前加入：

```html
<section id="scan-failure-panel" class="scan-failure-panel" hidden>
  <header class="scan-failure-head">
    <div>
      <p class="eyebrow">失败设备</p>
      <h3 id="scan-failure-title">采集失败明细</h3>
      <p id="scan-failure-summary" class="muted"></p>
    </div>
    <div class="scan-failure-actions">
      <button id="retry-failed-devices" class="button primary" type="button">
        重新采集失败设备
      </button>
      <button id="close-scan-failures" class="button secondary" type="button">
        关闭
      </button>
    </div>
  </header>
  <div class="scan-failure-tools">
    <input id="scan-failure-search"
           type="search"
           maxlength="200"
           placeholder="搜索设备名称、IP、集群或失败原因">
    <select id="scan-failure-page-size" aria-label="失败明细每页数量">
      <option value="10">10 条/页</option>
      <option value="20" selected>20 条/页</option>
      <option value="50">50 条/页</option>
    </select>
    <button id="refresh-scan-failures" class="button secondary" type="button">
      刷新
    </button>
  </div>
  <div id="scan-failure-error" class="inline-error" hidden></div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>设备名称</th><th>IP 地址</th><th>所属集群</th>
          <th>失败原因</th><th>开始时间</th><th>结束时间</th>
        </tr>
      </thead>
      <tbody id="scan-failure-body"></tbody>
    </table>
  </div>
  <footer class="scan-failure-pagination">
    <button id="scan-failure-prev" class="button secondary" type="button">上一页</button>
    <label>第
      <input id="scan-failure-page" type="number" min="1" value="1">
      页
    </label>
    <span id="scan-failure-pages">共 1 页</span>
    <button id="scan-failure-next" class="button secondary" type="button">下一页</button>
  </footer>
</section>
```

删除模板内原有 `batchTypeNames` 至 `loadScanBatches()` 的批次脚本代码，保留导入相关代码。将外部脚本放在 `device-list.js` 前：

```html
<script src="{{ url_for('static', path='js/scan-batches.js') }}?v=20260731-failures"></script>
<script src="{{ url_for('static', path='js/device-list.js') }}"></script>
```

- [ ] **Step 4: 实现批次卡片和面板状态管理**

创建 `app/static/js/scan-batches.js`。批次类型映射必须包含：

```javascript
const batchTypeNames = {
  all: "全部设备",
  cluster: "集群扫描",
  import: "导入首次扫描",
  retry: "失败重试"
};
```

卡片失败数大于 0 时加入：

```javascript
${batch.failed_tasks > 0 ? `
  <button class="text-link link-button"
          type="button"
          data-view-failures="${batch.id}">
    查看失败明细
  </button>` : ""}
```

使用单一状态对象，防止多个面板轮询：

```javascript
const failureState = {
  batchId: null,
  page: 1,
  pageSize: 20,
  query: "",
  pages: 1,
  status: null,
  timer: null,
  loading: false
};

const stopFailurePolling = () => {
  if (failureState.timer) clearTimeout(failureState.timer);
  failureState.timer = null;
};
```

- [ ] **Step 5: 实现失败明细加载、转义和北京时间**

实现时间格式：

```javascript
const formatBeijingTime = value => value
  ? new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false, timeZone: "Asia/Shanghai"
    }).format(new Date(value))
  : "—";
```

构造查询并请求：

```javascript
const params = new URLSearchParams({
  page: String(failureState.page),
  page_size: String(failureState.pageSize)
});
if (failureState.query) params.set("q", failureState.query);
const response = await fetch(
  `/api/scan-batches/${failureState.batchId}/failures?${params.toString()}`
);
```

每行所有外部文本必须调用 `escapeHtml`：

```javascript
failureBody.innerHTML = payload.items.length
  ? payload.items.map(item => `<tr>
      <td>${escapeHtml(item.device_name)}</td>
      <td>${escapeHtml(item.host)}</td>
      <td>${escapeHtml(item.cluster_name || "未分配")}</td>
      <td class="scan-failure-reason">${escapeHtml(item.error_message)}</td>
      <td>${formatBeijingTime(item.started_at)}</td>
      <td>${formatBeijingTime(item.finished_at)}</td>
    </tr>`).join("")
  : `<tr><td colspan="6" class="muted">当前条件下没有失败设备。</td></tr>`;
```

若 `failureState.page > payload.pages` 且 `payload.pages > 0`，把页码改为 `payload.pages` 并立即重新加载。仅当 `payload.batch_status !== "completed"` 且面板仍打开时设置 1500ms 定时器。

请求失败时：

- 不清空 `scan-failure-body`。
- 显示 `scan-failure-error`。
- 停止本轮自动刷新，但“刷新”按钮仍可再次调用。

- [ ] **Step 6: 实现搜索、分页、关闭与重试**

事件规则：

```javascript
document.getElementById("scan-batch-list").addEventListener("click", event => {
  const button = event.target.closest("[data-view-failures]");
  if (button) openFailurePanel(Number(button.dataset.viewFailures));
});
```

- 搜索输入使用 300ms 防抖，变化后将页码重置为 1。
- 页大小变化后将页码重置为 1。
- 上一页和下一页限制在 1 至 `failureState.pages`。
- 页码输入回车或失焦时取整并限制到有效范围。
- 关闭按钮调用 `stopFailurePolling()`，清空 `batchId` 并隐藏面板。

失败重试实现：

```javascript
const retryButton = document.getElementById("retry-failed-devices");
retryButton.addEventListener("click", async () => {
  if (!failureState.batchId || retryButton.disabled) return;
  retryButton.disabled = true;
  const original = retryButton.textContent;
  retryButton.textContent = "正在创建重试批次…";
  try {
    const response = await fetch(
      `/api/scan-batches/${failureState.batchId}/retry-failures`,
      {method: "POST"}
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "创建重试批次失败");
    toast(`失败设备已加入重试批次 #${result.id}`);
    await loadScanBatches();
    await loadFailurePage();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    retryButton.disabled = false;
    retryButton.textContent = original;
  }
});
```

将 `renderImportScanBatch` 暴露为：

```javascript
window.renderImportScanBatch = async (importBatchId, scanBatchId) => {
  const response = await fetch(`/api/scan-batches/${scanBatchId}`);
  if (!response.ok) return;
  const batch = await response.json();
  const target = document.getElementById(`import-scan-${importBatchId}`);
  if (!target) return;
  target.innerHTML = `<h3>首次完整扫描</h3>${batchCard(batch)}`;
  if (batch.status !== "completed") {
    window.setTimeout(
      () => window.renderImportScanBatch(importBatchId, scanBatchId),
      1500
    );
  }
};
```

- [ ] **Step 7: 增加响应式样式**

在 `app/static/css/app.css` 加入：

```css
.scan-failure-panel {
  margin: 0 22px 22px;
  border: 1px solid var(--line);
  background: #091316;
}
.scan-failure-head,
.scan-failure-tools,
.scan-failure-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
}
.scan-failure-head,
.scan-failure-tools { border-bottom: 1px solid var(--line); }
.scan-failure-actions { display: flex; gap: 8px; }
.scan-failure-tools input { min-width: 280px; flex: 1; }
.scan-failure-reason { color: var(--danger); min-width: 220px; }
.scan-failure-pagination { justify-content: flex-end; }
.scan-failure-pagination input { width: 72px; }
.inline-error { margin: 12px 16px 0; color: var(--danger); }
```

在现有移动端媒体查询中加入：

```css
.scan-failure-head,
.scan-failure-tools,
.scan-failure-pagination {
  align-items: stretch;
  flex-direction: column;
}
.scan-failure-tools input { min-width: 0; width: 100%; }
.scan-failure-actions { flex-wrap: wrap; }
```

- [ ] **Step 8: 运行前端测试和 JavaScript 语法检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_batch_frontend.py tests\test_device_list_frontend.py -q
node --check app\static\js\scan-batches.js
```

Expected: 全部 PASS，Node 无输出且退出码为 0。

---

### Task 4: 回归、浏览器验收、文档与打包

**Files:**
- Modify: `README.md`
- Verify: all application and test files
- Produce: `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

**Interfaces:**
- Consumes: 完整失败明细和失败重试功能。
- Produces: 可验证页面、绿色测试结果和带时间戳 Linux 部署包。

- [ ] **Step 1: 更新 README**

在批量扫描说明中补充：

```markdown
批量扫描卡片会持续显示等待、执行中、成功和失败数量。失败数大于 0 时，
可打开独立失败明细面板，按设备名称、IP、集群或失败原因搜索并分页查看；
执行中的批次会自动刷新，完成后仍可回看。点击“重新采集失败设备”会创建
新的失败重试批次，原批次历史保持不变。
```

- [ ] **Step 2: 运行后端和前端相关回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scan_batch_failures.py tests\test_scan_queue_api.py tests\test_scan_queue.py tests\test_scan_batch_frontend.py tests\test_device_list_frontend.py -q
```

Expected: PASS。

- [ ] **Step 3: 运行完整测试和静态检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts
node --check app\static\js\scan-batches.js
node --check app\static\js\device-list.js
node --check app\static\js\topology.js
```

Expected: 所有测试 PASS；Ruff 显示 `All checks passed!`；Node 命令退出码均为 0。

- [ ] **Step 4: 重启本地服务并执行浏览器验收**

使用当前项目虚拟环境重启 Uvicorn，确保实际加载最新代码。仅创建本次验收所需的临时设备、批次、任务和批次成员；验收后删除这些临时测试数据。

浏览器检查：

1. 设备管理页批次卡片失败数大于 0 时显示“查看失败明细”。
2. 点击后在批次区下方打开独立面板。
3. 设备名称、IP、集群、失败原因和北京时间正确。
4. 搜索和 10/20/50 每页切换正确。
5. 上一页、下一页和页码跳转正确。
6. 执行中批次自动刷新；完成后停止明细轮询。
7. 点击重试只产生一个新“失败重试”批次，按钮提交期间禁用。
8. 关闭面板后不再请求失败明细。
9. 页面控制台没有新增 error 或 warning。

- [ ] **Step 5: 生成带时间戳 Linux 包**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
```

Expected: 输出新的 `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`。

- [ ] **Step 6: 检查压缩包内容**

用 `tar -tzf` 检查：

- 必须包含 `app/services/scan_batch_failures.py`。
- 必须包含 `app/static/js/scan-batches.js`。
- 必须包含更新后的模板、样式、模型、路由、模式和 README。
- 不得包含 `.git`、`.venv`、`tests`、`__pycache__`、`.pytest_cache` 或数据库文件。
- 如果包含 `.env`，在交付说明中明确安全风险。
- 如果不包含 `wheelhouse`，在交付说明中明确内网完全离线部署需另备依赖包。

- [ ] **Step 7: 最终交付说明**

报告：

- 失败明细和失败重试行为。
- 完整测试数量和静态检查结果。
- 浏览器验收结果。
- 压缩包绝对路径。
- `.env` 与 `wheelhouse` 状态。
- 明确说明未执行任何 Git 操作。
