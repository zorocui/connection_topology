# 分级历史保留策略实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将默认历史保留时间改为 7 天，并实现设备 > 集群 > 系统三级可继承保留策略。

**Architecture:** 在设备和集群模型上保存可空覆盖值，由独立策略服务计算实际生效值。每日清理按有效天数对设备分组后批量删除过期扫描，并在成功后清空拓扑缓存。接口同时返回自定义值与实际值，页面空值表示继承。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、SQLite、Jinja2、原生 JavaScript、pytest。

## Global Constraints

- 保留期范围为 1～3650 天，设备和集群的 `null` 表示继承。
- 优先级固定为设备 > 集群 > 系统，系统默认值为 7 天。
- 数据库结构版本升级到 6；旧系统值恰好为 30 时迁移为 7，其他值不覆盖。
- Excel 导入格式本阶段不增加字段。
- 不增加第三方依赖，不改变拓扑 1d、3d、7d 查询语义。
- 用户已要求不使用 Git；所有 Git 提交步骤均省略。

---

### Task 1: 数据模型、默认值和数据库迁移

**Files:**
- Modify: `app/models.py`
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `app/migrations.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Produces: `Device.history_retention_days: int | None`
- Produces: `Cluster.history_retention_days: int | None`
- Produces: schema version 6 and system default 7

- [ ] **Step 1: Write failing default and migration tests**

Add assertions that a fresh `Settings()` has `history_retention_days == 7`; migration adds both nullable columns, records schema version 6, changes an existing system value of 30 to 7, and preserves an existing value of 45.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_config.py tests\test_migrations.py -q
```

Expected: failures for the old default, missing columns, and schema version 5.

- [ ] **Step 3: Add model fields and migration**

Add to both `Cluster` and `Device`:

```python
history_retention_days: Mapped[int | None] = mapped_column(
    Integer,
    nullable=True,
)
```

Change all application fallback defaults from 30 to 7. Set
`LATEST_SCHEMA_VERSION = 6` and add guarded SQLite migrations:

```python
if "history_retention_days" not in cluster_columns:
    connection.execute(
        text("ALTER TABLE clusters ADD COLUMN history_retention_days INTEGER")
    )
if "history_retention_days" not in device_columns:
    connection.execute(
        text("ALTER TABLE devices ADD COLUMN history_retention_days INTEGER")
    )
if "system_settings" in inspector.get_table_names():
    connection.execute(
        text(
            "UPDATE system_settings SET history_retention_days = 7 "
            "WHERE history_retention_days = 30"
        )
    )
```

- [ ] **Step 4: Run focused tests**

Run the command from Step 2. Expected: all tests pass.

---

### Task 2: 保留策略解析与分组清理

**Files:**
- Create: `app/services/retention.py`
- Modify: `app/services/scheduler.py`
- Modify: `app/main.py`
- Modify: `tests/test_services.py`
- Create: `tests/test_retention.py`

**Interfaces:**
- Produces: `resolve_device_retention(device, system_days) -> RetentionPolicy`
- Produces: `purge_expired_scans(session, system_days, now=None) -> int`
- Consumes: `TopologyCache.clear()`

- [ ] **Step 1: Write failing priority and cleanup tests**

Cover these exact cases:

```python
assert resolve_device_retention(device_override, 7).days == 30
assert resolve_device_retention(cluster_override_device, 7).days == 14
assert resolve_device_retention(inheriting_device, 7).days == 7
```

Seed two devices with 2-day and 10-day policies plus old/recent scans. Invoke cleanup at a fixed `now`; assert each device retains only records inside its own cutoff and related `ConnectionRecord` rows are cascaded.

- [ ] **Step 2: Verify the tests fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_retention.py tests\test_services.py -q
```

Expected: import or signature failures.

- [ ] **Step 3: Implement the focused policy service**

Create:

```python
@dataclass(frozen=True)
class RetentionPolicy:
    days: int
    source: Literal["device", "cluster", "system"]


def resolve_device_retention(
    device: Device,
    system_days: int,
) -> RetentionPolicy:
    if device.history_retention_days is not None:
        return RetentionPolicy(device.history_retention_days, "device")
    if device.cluster and device.cluster.history_retention_days is not None:
        return RetentionPolicy(device.cluster.history_retention_days, "cluster")
    return RetentionPolicy(system_days, "system")
```

Implement cleanup by loading devices with `selectinload(Device.cluster)`, grouping IDs by effective day count, then executing one `DELETE ScanRun` per day group:

```python
for retention_days, device_ids in groups.items():
    cutoff = reference - timedelta(days=retention_days)
    result = session.execute(
        delete(ScanRun).where(
            ScanRun.device_id.in_(device_ids),
            ScanRun.started_at < cutoff,
        )
    )
    deleted += result.rowcount or 0
session.commit()
```

The scheduler reads the system setting with fallback 7, invokes cleanup, and clears topology cache only after successful commit. Pass the existing cache-clear callback from application startup.

- [ ] **Step 4: Run focused tests**

Run the command from Step 2. Expected: all pass.

---

### Task 3: 集群和设备 API

**Files:**
- Modify: `app/schemas.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_cluster_api.py`

**Interfaces:**
- Produces cluster JSON fields: `history_retention_days`, `effective_history_retention_days`
- Produces device JSON fields: `history_retention_days`, `effective_history_retention_days`, `history_retention_source`

- [ ] **Step 1: Write failing API tests**

Test:

- cluster create/update accepts `null`, 14, and rejects 0/3651;
- device create/update accepts `null`, 30, and rejects 0/3651;
- device response reports source `device`, `cluster`, then `system` as overrides are cleared;
- cluster response reports its override or current system default.

- [ ] **Step 2: Verify failures**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_api.py tests\test_cluster_api.py -q
```

- [ ] **Step 3: Extend schemas and response builders**

Add optional request fields:

```python
history_retention_days: int | None = Field(default=None, ge=1, le=3650)
```

Add read fields:

```python
history_retention_days: int | None
effective_history_retention_days: int
history_retention_source: Literal["device", "cluster", "system"]
```

Cluster reads omit `history_retention_source` and compute the effective value from its override or system default. Device reads use `resolve_device_retention`. Ensure update routes distinguish an explicitly supplied `null` from an omitted field by checking `payload.model_fields_set`.

- [ ] **Step 4: Run API tests**

Run the command from Step 2. Expected: all pass.

---

### Task 4: 集群和设备管理页面

**Files:**
- Modify: `app/templates/clusters.html`
- Modify: `app/static/js/clusters.js`
- Modify: `app/templates/devices.html`
- Modify: `app/static/js/device-list.js`
- Modify: `app/static/css/app.css`
- Modify: `tests/test_topology_frontend.py` or create `tests/test_retention_frontend.py`

**Interfaces:**
- Consumes API fields from Task 3
- Sends blank form values as JSON `null`

- [ ] **Step 1: Write frontend contract tests**

Assert templates contain retention number inputs with `min="1"` and `max="3650"`, and JavaScript contains blank-to-null conversion:

```javascript
const retentionValue = input.value.trim();
history_retention_days: retentionValue ? Number(retentionValue) : null
```

- [ ] **Step 2: Verify tests fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_retention_frontend.py -q
```

- [ ] **Step 3: Implement forms and effective-value labels**

Add a nullable retention input to cluster create/edit and device create/edit forms. Display:

```text
留空时继承上级设置
实际生效：7 天（系统）
实际生效：14 天（集群）
实际生效：30 天（设备）
```

When editing, populate the custom value only; do not write the effective inherited value into the input. Update asset query strings if templates use explicit cache versions.

- [ ] **Step 4: Run frontend tests and JavaScript syntax checks**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_retention_frontend.py -q
node --check app\static\js\clusters.js
node --check app\static\js\device-list.js
```

Expected: all pass with no syntax output.

---

### Task 5: 系统页面、文档、回归与部署包

**Files:**
- Modify: `app/routes/pages.py`
- Modify: `app/templates/settings.html`
- Modify: `README.md`
- Verify: `package-linux.ps1`

**Interfaces:**
- Consumes all previous tasks
- Produces a timestamped `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

- [ ] **Step 1: Update system fallback and documentation**

Change remaining fallback text and code from 30 to 7. Document the priority:

```text
设备自定义保留期 > 集群自定义保留期 > 系统默认 7 天
```

Document that leaving device or cluster retention blank restores inheritance.

- [ ] **Step 2: Run full validation**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests scripts
node --check app\static\js\clusters.js
node --check app\static\js\device-list.js
node --check app\static\js\topology.js
```

Expected: full suite passes, Ruff passes, and Node emits no syntax errors.

- [ ] **Step 3: Restart and browser-test**

Restart the verified workspace Uvicorn process. In the local browser verify:

- system setting initially displays 7;
- cluster blank/custom/blank transitions show correct effective value;
- device blank/custom/blank transitions show system or cluster inheritance;
- no browser console errors.

Use temporary acceptance data only if necessary and delete it after verification.

- [ ] **Step 4: Generate and inspect timestamped package**

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
tar -tzf .\connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz
```

Verify the archive contains updated application files and excludes `.git`, `.venv`, databases, caches, and test fixtures. Report whether `.env` and `wheelhouse` are included.

