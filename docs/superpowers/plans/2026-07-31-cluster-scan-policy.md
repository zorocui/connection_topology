# 集群统一采集策略实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许管理员在集群管理中统一设置采集间隔和定时采集开关，并同步到当前及以后加入该集群的全部设备。

**Architecture:** 集群保存权威采集策略，设备表保存调度器实际使用的同步副本。集群更新在事务内批量覆盖成员设备，提交后调用现有调度服务刷新任务；设备创建、导入和换组在写入时复制目标集群策略。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、SQLite、APScheduler、Jinja2、原生 JavaScript、pytest。

## Global Constraints

- 集群采集间隔范围为 1～10080 分钟，默认 5 分钟。
- 集群定时采集默认启用；关闭只影响自动任务，不影响立即手动采集。
- 数据库结构版本升级到 7。
- Excel 模板字段保持不变；已分组设备忽略行内采集策略并采用集群值。
- 删除集群或移出集群后，设备保留最后同步到设备表的实际设置。
- 不增加第三方依赖，保持 Python 3.10。
- 用户已要求不使用 Git；省略全部 Git 操作。

---

### Task 1: 集群模型与版本 7 迁移

**Files:**
- Modify: `app/models.py`
- Modify: `app/migrations.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Produces: `Cluster.scan_interval_minutes: int`
- Produces: `Cluster.scheduled_enabled: bool`
- Produces: schema version 7

- [ ] **Step 1: Write failing migration tests**

创建旧版数据库数据，覆盖：

```python
# 集群 1 的设备间隔为 5、10、10，结果为 10
# 集群 2 的设备间隔为 5、10，平局结果为 5
# 只有全部设备 scheduled_enabled=False 时集群结果才为 False
# 空集群结果为 5 / True
```

断言 `clusters` 新增两个非空字段且最高结构版本为 7。

- [ ] **Step 2: Verify tests fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_migrations.py -q
```

Expected: missing cluster columns and expected version 7 failures.

- [ ] **Step 3: Implement model and idempotent migration**

Add:

```python
scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
scheduled_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
```

For an existing table, add SQLite columns with non-null defaults:

```sql
ALTER TABLE clusters
ADD COLUMN scan_interval_minutes INTEGER NOT NULL DEFAULT 5;
ALTER TABLE clusters
ADD COLUMN scheduled_enabled BOOLEAN NOT NULL DEFAULT 1;
```

Only when version 7 has not been applied, update each existing cluster using deterministic aggregate queries. For scheduled state use `MIN(CAST(devices.scheduled_enabled AS INTEGER))`; for interval use a grouped count ordered by `COUNT(*) DESC, scan_interval_minutes ASC LIMIT 1`.

- [ ] **Step 4: Run migration tests**

Run the command from Step 2. Expected: all pass.

---

### Task 2: 集群策略同步服务与 API

**Files:**
- Modify: `app/schemas.py`
- Modify: `app/services/clusters.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_clusters.py`
- Modify: `tests/test_scheduler_queue.py`

**Interfaces:**
- Produces: `apply_cluster_scan_policy(session, cluster) -> list[Device]`
- Produces cluster API fields: `scan_interval_minutes`, `scheduled_enabled`
- Consumes: `SchedulerService.sync_device(device)`

- [ ] **Step 1: Write failing service and API tests**

Test that updating one cluster to 12 minutes/disabled:

```python
assert [(d.scan_interval_minutes, d.scheduled_enabled) for d in members] == [
    (12, False),
    (12, False),
]
assert unrelated.scan_interval_minutes == 5
assert unrelated.scheduled_enabled is True
```

Verify interval 0 and 10081 return 422. Use a fake scheduler to assert each affected member ID is synchronized exactly once after commit and no sync occurs if commit fails.

- [ ] **Step 2: Verify tests fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_clusters.py tests\test_scheduler_queue.py -q
```

- [ ] **Step 3: Implement policy update and synchronization**

Extend cluster schemas:

```python
scan_interval_minutes: int = Field(default=5, ge=1, le=10080)
scheduled_enabled: bool = True
```

Create:

```python
def apply_cluster_scan_policy(
    session: Session,
    cluster: Cluster,
) -> list[Device]:
    members = list(
        session.scalars(
            select(Device).where(Device.cluster_id == cluster.id)
        )
    )
    for device in members:
        device.scan_interval_minutes = cluster.scan_interval_minutes
        device.scheduled_enabled = cluster.scheduled_enabled
    return members
```

In the cluster update route set the cluster fields, call this function before commit, then after successful commit call `scheduler.sync_device` for every returned device. Catch and log individual scheduler exceptions without skipping later devices.

- [ ] **Step 4: Run focused tests**

Run the command from Step 2. Expected: all pass.

---

### Task 3: 新设备、换组和 Excel 导入采用集群策略

**Files:**
- Modify: `app/services/clusters.py`
- Modify: `app/services/imports.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_clusters.py`
- Modify: `tests/test_imports.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces: `cluster_scan_values(cluster, requested_interval, requested_enabled) -> tuple[int, bool]`
- Consumes: cluster fields from Task 1

- [ ] **Step 1: Write failing assignment and import tests**

Cover:

- normal device create into a 20-minute disabled cluster ignores submitted 3/true;
- ungrouped device keeps submitted 3/true;
- moving an existing device into the cluster changes it to 20/false and resynchronizes it;
- Excel row assigned to that cluster ignores row interval/enable values;
- quick-created cluster uses 5/true;
- removing a device from a cluster preserves its last 20/false.

- [ ] **Step 2: Verify tests fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_clusters.py tests\test_imports.py tests\test_api.py -q
```

- [ ] **Step 3: Implement one shared resolver**

Add:

```python
def cluster_scan_values(
    cluster: Cluster | None,
    requested_interval: int,
    requested_enabled: bool,
) -> tuple[int, bool]:
    if cluster is None:
        return requested_interval, requested_enabled
    return cluster.scan_interval_minutes, cluster.scheduled_enabled
```

Use it in the device create route, device update route when `cluster_id` or `new_cluster_name` is supplied, and `import_devices`. Do not call it when an update omits cluster fields, so ordinary device edits do not unexpectedly overwrite values.

- [ ] **Step 4: Run focused tests**

Run the command from Step 2. Expected: all pass.

---

### Task 4: 集群管理和设备提示页面

**Files:**
- Modify: `app/templates/clusters.html`
- Modify: `app/static/js/clusters.js`
- Modify: `app/templates/devices.html`
- Modify: `app/static/css/app.css`
- Modify: `tests/test_clusters_frontend.py`

**Interfaces:**
- Consumes cluster API fields from Task 2
- Sends integer `scan_interval_minutes` and boolean `scheduled_enabled`

- [ ] **Step 1: Write failing frontend contract tests**

Assert the cluster form contains a `min="1" max="10080"` interval input and a scheduled checkbox; JavaScript populates both when editing and includes both fields in JSON. Assert device rows belonging to a cluster display “由集群统一管理”.

- [ ] **Step 2: Verify tests fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_clusters_frontend.py -q
```

- [ ] **Step 3: Implement the page**

Add cluster fields:

```html
<input id="cluster-scan-interval" type="number"
  min="1" max="10080" value="5" required>
<input id="cluster-scheduled-enabled" type="checkbox" checked>
```

Render card summary:

```text
每 N 分钟采集 · 定时采集已启用/已关闭
```

On reset restore 5/checked; on edit load API values; on submit send:

```javascript
scan_interval_minutes: Number(scanIntervalInput.value),
scheduled_enabled: scheduledInput.checked
```

Add the device cluster-managed hint next to its current interval.

- [ ] **Step 4: Run frontend tests and syntax check**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_clusters_frontend.py -q
node --check app\static\js\clusters.js
```

Expected: tests pass and Node produces no syntax errors.

---

### Task 5: 回归、运行态验收和部署包

**Files:**
- Modify: `README.md`
- Verify: `package-linux.ps1`

**Interfaces:**
- Produces: timestamped Linux archive

- [ ] **Step 1: Update documentation**

Document that cluster settings immediately cover all current members and future members, while ungrouped devices retain individual settings.

- [ ] **Step 2: Run full validation**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests scripts
node --check app\static\js\clusters.js
node --check app\static\js\device-list.js
node --check app\static\js\topology.js
```

Expected: all tests and checks pass.

- [ ] **Step 3: Restart and browser-test**

After verifying the port-8000 process belongs to this workspace, restart Uvicorn so migration 7 runs. In the browser:

- edit an existing cluster to a temporary interval and disabled state;
- verify the card and member device row both update;
- verify the scheduler job is removed through a read-only application diagnostic or test seam;
- restore the original cluster values;
- confirm browser console has no errors.

- [ ] **Step 4: Package and inspect**

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
tar -tzf .\connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz
```

Verify required changed files exist and `.git`, `.venv`, databases, caches, and tests are absent. Report `.env` and `wheelhouse` inclusion.

