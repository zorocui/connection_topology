# Passwordless Cluster Membership Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Excel imports to create cluster-only marker devices without passwords, while guaranteeing those devices appear as cluster members but are never collected until valid credentials are saved.

**Architecture:** Add an explicit `Device.collection_enabled` capability flag, defaulting to `true` for all existing and normally-created devices. Passwordless import rows create cluster members with an encrypted empty password and `collection_enabled=false`; every scan entry point and the scan executor enforce that flag. Supplying a valid password through the existing device update API upgrades a marker into a collectible device.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy 2, SQLite migrations, Pydantic 2, APScheduler, OpenPyXL, Jinja2, pytest.

## Global Constraints

- Database schema version becomes exactly `9`.
- `Device.encrypted_password` remains non-null; marker devices store `cipher.encrypt("")` and never expose plaintext or ciphertext.
- A blank-password row is valid only when `所属集群` is non-empty; all other existing required fields remain required.
- Exact duplicate identity remains `host + port + username`.
- A passwordless duplicate is an import error, does not modify the existing device, and logs batch ID, Excel row number, device name, host, port, username, and conflict reason—never password material.
- A normal password-bearing duplicate retains the existing skipped-row behavior.
- Marker rows never enter connection testing, import first-scan batches, scheduled scans, manual scans, all-device scans, or cluster scans.
- Manual scan of a marker returns HTTP `409` with exact detail `该设备仅用于集群标注，未配置采集凭据`.
- Request schemas must not expose `collection_enabled` for direct client assignment.
- Saving a non-empty password enables collection only after the existing connection test succeeds; a failed test leaves the device unchanged.
- Cluster topology includes marker devices as cluster members and does not synthesize connection edges for them.

---

## File Map

- `app/models.py`: persistent collection capability on `Device`.
- `app/migrations.py`: schema version 9, legacy-safe column addition and backfill.
- `app/schemas.py`: read-only API exposure of collection capability.
- `app/services/imports.py`: template guidance, conditional password validation, marker creation, duplicate-error logging.
- `app/services/scheduler.py`: prevent marker scheduling and remove stale marker jobs.
- `app/services/scan_queue.py`: reject direct marker enqueue and filter marker devices from batches.
- `app/services/scans.py`: final execution-time race/legacy-task guard.
- `app/routes/api.py`: response mapping, manual/batch filtering, credential-based upgrade.
- `app/templates/devices.html`: marker label and disabled immediate-scan control.
- `tests/test_migrations.py`, `tests/test_imports.py`, `tests/test_scheduler_queue.py`, `tests/test_scan_queue.py`, `tests/test_scan_queue_api.py`, `tests/test_services.py`, `tests/test_api.py`, `tests/test_cluster_topology.py`, `tests/test_device_list_frontend.py`: focused regression coverage.

### Task 1: Persist and expose collection capability

**Files:**
- Modify: `app/models.py`
- Modify: `app/migrations.py`
- Modify: `app/schemas.py`
- Modify: `app/routes/api.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `Device.collection_enabled: bool`, persisted as non-null boolean with Python and database default `true`.
- Produces: `DeviceRead.collection_enabled: bool` through `_device_read(device, system_days)`.
- Does not add the field to `DeviceCreate` or `DeviceUpdate`.

- [ ] **Step 1: Add failing migration and API response tests**

Add to `tests/test_migrations.py`:

```python
def test_existing_devices_are_collection_enabled_after_v9_upgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'collection-v9.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE devices ("
                "id INTEGER PRIMARY KEY, name VARCHAR(100), "
                "encrypted_password TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO devices (id, name, encrypted_password) "
                "VALUES (1, 'legacy', 'ciphertext')"
            )
        )

    init_database(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("devices")}
    assert "collection_enabled" in columns
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT collection_enabled FROM devices WHERE id = 1")
        ).scalar() == 1
        assert connection.execute(
            text("SELECT MAX(version) FROM schema_versions")
        ).scalar() == 9
```

Extend the create/list assertions in `tests/test_api.py`:

```python
assert body["collection_enabled"] is True
assert listed[0]["collection_enabled"] is True
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_migrations.py::test_existing_devices_are_collection_enabled_after_v9_upgrade tests/test_api.py::test_device_workflow_never_returns_password -v`

Expected: FAIL because the database column and response field do not exist.

- [ ] **Step 3: Add the model field, migration, and read mapping**

In `app/models.py`, place this beside `scheduled_enabled`:

```python
collection_enabled: Mapped[bool] = mapped_column(
    Boolean,
    nullable=False,
    default=True,
    server_default="1",
)
```

In `app/migrations.py`, change the version and add the idempotent device upgrade:

```python
LATEST_SCHEMA_VERSION = 9
```

```python
if "collection_enabled" not in device_columns:
    connection.execute(
        text(
            "ALTER TABLE devices ADD COLUMN "
            "collection_enabled BOOLEAN NOT NULL DEFAULT 1"
        )
    )
```

In `app/schemas.py`, add only to `DeviceRead`:

```python
collection_enabled: bool
```

In `app/routes/api.py`, add to `_device_read`:

```python
collection_enabled=device.collection_enabled,
```

- [ ] **Step 4: Run model/API regressions**

Run: `pytest tests/test_migrations.py tests/test_api.py -v`

Expected: PASS, including legacy row value `1` and API value `true`.

- [ ] **Step 5: Commit the persistence boundary**

```bash
git add app/models.py app/migrations.py app/schemas.py app/routes/api.py tests/test_migrations.py tests/test_api.py
git commit -m "feat: add device collection capability"
```

### Task 2: Import cluster-only marker devices and report duplicate errors

**Files:**
- Modify: `app/services/imports.py`
- Test: `tests/test_imports.py`

**Interfaces:**
- Consumes: `Device.collection_enabled` from Task 1.
- Produces: `_parse_row(values)` with optional `password: str` and the invariant `password or cluster_name`.
- Produces: passwordless imported rows with `ImportStatus.IMPORTED`, `ImportTestStatus.NOT_APPLICABLE`, and exact message `仅标注集群设备，不执行连接测试`.
- Produces: passwordless duplicate rows with `ImportStatus.ERROR` and exact message `设备已存在，未修改集群归属`.

- [ ] **Step 1: Add failing template, validation, marker, mixed-batch, and duplicate-log tests**

Update the template test in `tests/test_imports.py`:

```python
assert "可留空" in instructions["密码"]
assert "所属集群" in instructions["密码"]
```

Add these tests (reuse `workbook_bytes`):

```python
def test_passwordless_row_requires_cluster(app):
    content = workbook_bytes([
        ("marker", "10.0.0.70", "linux", 22, "ops", "", "", 5, "否")
    ])
    with app.state.session_factory() as session:
        batch = import_devices(session, app.state.cipher, "devices.xlsx", content)
        assert batch.error_rows == 1
        assert batch.imported_rows == 0
        assert batch.rows[0].import_message == "未填写密码时必须填写所属集群"


def test_passwordless_cluster_row_creates_non_collectible_member(app):
    content = workbook_bytes([
        ("marker", "10.0.0.71", "linux", 22, "ops", "", "marker-cluster", 5, "是")
    ])
    with app.state.session_factory() as session:
        batch = import_devices(session, app.state.cipher, "devices.xlsx", content)
        device = session.scalar(select(Device).where(Device.host == "10.0.0.71"))
        assert batch.status.value == "completed"
        assert batch.imported_rows == 1
        assert batch.test_pending_rows == 0
        assert device.collection_enabled is False
        assert app.state.cipher.decrypt(device.encrypted_password) == ""
        assert device.cluster.name == "marker-cluster"
        assert batch.rows[0].test_status == ImportTestStatus.NOT_APPLICABLE
        assert batch.rows[0].import_message == "仅标注集群设备，不执行连接测试"


def test_mixed_import_tests_only_password_devices(app):
    content = workbook_bytes([
        ("real", "10.0.0.72", "linux", 22, "ops", "secret", "mixed", 5, "是"),
        ("marker", "10.0.0.73", "linux", 22, "ops", "", "mixed", 5, "是"),
    ])
    with app.state.session_factory() as session:
        batch = import_devices(session, app.state.cipher, "devices.xlsx", content)
        assert batch.test_pending_rows == 1
        assert batch.status.value == "testing"
        statuses = {row.device_name: row.test_status for row in batch.rows}
        assert statuses == {
            "real": ImportTestStatus.PENDING,
            "marker": ImportTestStatus.NOT_APPLICABLE,
        }


def test_passwordless_duplicate_is_logged_and_does_not_modify_device(
    app, caplog
):
    content = workbook_bytes([
        ("marker-copy", "10.0.0.74", "linux", 22, "ops", "", "new-cluster", 5, "否")
    ])
    with app.state.session_factory() as session:
        original = Device(
            name="original",
            host="10.0.0.74",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(original)
        session.commit()
        original_id = original.id

        with caplog.at_level("WARNING", logger="app.services.imports"):
            batch = import_devices(session, app.state.cipher, "devices.xlsx", content)

        session.refresh(original)
        assert batch.error_rows == 1
        assert batch.skipped_rows == 0
        assert batch.rows[0].import_status == ImportStatus.ERROR
        assert batch.rows[0].import_message == "设备已存在，未修改集群归属"
        assert batch.rows[0].device_id == original_id
        assert original.cluster_id is None
        assert original.name == "original"
        log_text = caplog.text
        for expected in (
            f"batch_id={batch.id}", "row=2", "name=marker-copy",
            "host=10.0.0.74", "port=22", "username=ops",
            "设备已存在，未修改集群归属",
        ):
            assert expected in log_text
        assert "secret" not in log_text
        assert original.encrypted_password not in log_text
```

Add `OSType` to the existing model imports in the test.

- [ ] **Step 2: Run the new tests and verify failure**

Run: `pytest tests/test_imports.py -k "passwordless or mixed_import or template" -v`

Expected: FAIL because blank passwords are currently rejected and no marker/log behavior exists.

- [ ] **Step 3: Make password conditional and document it in the workbook**

Add `import logging` and `logger = logging.getLogger(__name__)` in `app/services/imports.py`.

Change the password instruction to:

```python
(
    "密码",
    "条件必填",
    "正常采集设备必须填写；仅用于集群标注的设备可留空，但必须填写所属集群。",
),
```

In `_parse_row`, parse cluster/password before returning:

```python
password = _text(values[5], "密码", required=False, max_length=1024)
cluster_name = _text(values[6], "所属集群", required=False, max_length=100)
if not password and not cluster_name:
    raise ImportValidationError("未填写密码时必须填写所属集群")
```

Return those variables as `"password": password` and `"cluster_name": cluster_name` while leaving all other validation unchanged.

- [ ] **Step 4: Branch duplicate and device creation behavior by password presence**

Replace the duplicate branch with:

```python
if duplicate:
    if not parsed["password"]:
        message = "设备已存在，未修改集群归属"
        session.add(
            ImportRowResult(
                batch_id=batch.id,
                row_number=row_number,
                device_name=device_name,
                host=host,
                device_id=duplicate.id,
                import_status=ImportStatus.ERROR,
                import_message=message,
                test_status=ImportTestStatus.NOT_APPLICABLE,
            )
        )
        batch.error_rows += 1
        logger.warning(
            "集群标注导入冲突 batch_id=%s row=%s name=%s host=%s "
            "port=%s username=%s reason=%s",
            batch.id,
            row_number,
            parsed["name"],
            parsed["host"],
            parsed["port"],
            parsed["username"],
            message,
        )
    else:
        session.add(
            ImportRowResult(
                batch_id=batch.id,
                row_number=row_number,
                device_name=device_name,
                host=host,
                device_id=duplicate.id,
                import_status=ImportStatus.SKIPPED,
                import_message="设备已存在，已跳过",
                test_status=ImportTestStatus.NOT_APPLICABLE,
            )
        )
        batch.skipped_rows += 1
    session.commit()
    continue
```

Before constructing the new device, derive:

```python
collection_enabled = bool(parsed["password"])
test_status = (
    ImportTestStatus.PENDING
    if collection_enabled
    else ImportTestStatus.NOT_APPLICABLE
)
import_message = (
    "导入成功，等待连接测试"
    if collection_enabled
    else "仅标注集群设备，不执行连接测试"
)
```

Set the device and row fields:

```python
encrypted_password=cipher.encrypt(parsed["password"]),
collection_enabled=collection_enabled,
```

```python
import_message=import_message,
test_status=test_status,
```

Increment pending tests only for collectible devices:

```python
batch.imported_rows += 1
if collection_enabled:
    batch.test_pending_rows += 1
```

- [ ] **Step 5: Run the full import suites**

Run: `pytest tests/test_imports.py tests/test_import_testing.py -v`

Expected: PASS. Existing password-bearing duplicate stays skipped; marker-only batch is immediately completed; mixed batch has one pending test.

Also add this explicit first-scan exclusion test to `tests/test_import_testing.py` (add `ImportStatus` to its model imports):

```python
def test_import_first_scan_batch_excludes_not_applicable_marker(app):
    with app.state.session_factory() as session:
        batch = ImportBatch(
            filename="markers.xlsx",
            status=ImportBatchStatus.COMPLETED,
            total_rows=2,
            imported_rows=2,
        )
        normal = Device(
            name="normal",
            host="10.0.0.75",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        marker = Device(
            name="marker",
            host="10.0.0.76",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(""),
            collection_enabled=False,
        )
        session.add_all([batch, normal, marker])
        session.flush()
        session.add_all([
            ImportRowResult(
                batch_id=batch.id,
                row_number=2,
                device_id=normal.id,
                import_status=ImportStatus.IMPORTED,
                test_status=ImportTestStatus.SUCCESS,
            ),
            ImportRowResult(
                batch_id=batch.id,
                row_number=3,
                device_id=marker.id,
                import_status=ImportStatus.IMPORTED,
                test_status=ImportTestStatus.NOT_APPLICABLE,
            ),
        ])
        session.commit()
        batch_id = batch.id
        normal_id = normal.id

    scan_batch = app.state.scan_queue.create_import_scan_batch(batch_id)
    with app.state.session_factory() as session:
        persisted = session.get(ScanBatch, scan_batch.id)
        assert persisted.total_tasks == 1
        assert [item.device_id for item in persisted.items] == [normal_id]
```

This explicitly proves the import callback does not create a first-scan task for a marker row.

- [ ] **Step 6: Commit import behavior**

```bash
git add app/services/imports.py tests/test_imports.py
git commit -m "feat: import passwordless cluster markers"
```

### Task 3: Enforce marker exclusion across every scan path

**Files:**
- Modify: `app/services/scheduler.py`
- Modify: `app/services/scan_queue.py`
- Modify: `app/services/scans.py`
- Modify: `app/routes/api.py`
- Test: `tests/test_scheduler_queue.py`
- Test: `tests/test_scan_queue.py`
- Test: `tests/test_scan_queue_api.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Produces: `CollectionDisabled(RuntimeError)` in `app.services.scans`.
- Produces: `DeviceCollectionDisabled(RuntimeError)` in `app.services.scan_queue`, with exact user-safe message.
- `ScanQueueService.enqueue_device(...)` rejects non-collectible devices.
- `_create_batch_in_session(...)` silently omits non-collectible IDs and still returns a completed zero-task batch when none qualify.
- `ScanService.run(...)` raises `CollectionDisabled` before creating a `ScanRun` or invoking a collector.

- [ ] **Step 1: Add failing scheduler and queue tests**

Add to `tests/test_scheduler_queue.py`:

```python
def test_marker_device_has_no_scheduled_job(app):
    queue = RecordingQueue()
    scheduler = SchedulerService(app.state.session_factory, queue, 123)
    device = Device(
        id=100,
        name="marker",
        host="10.0.0.100",
        os_type=OSType.LINUX,
        port=22,
        username="ops",
        encrypted_password="unused",
        scan_interval_minutes=5,
        scheduled_enabled=True,
        collection_enabled=False,
    )
    scheduler.sync_device(device)
    assert scheduler.scheduler.get_job("device-scan-100") is None
```

In `tests/test_scan_queue.py`, add a helper-created marker device and assert:

```python
with pytest.raises(DeviceCollectionDisabled, match="仅用于集群标注"):
    queue.enqueue_device(marker_id, ScanTrigger.MANUAL, PRIORITY_MANUAL)

batch = queue.create_batch(ScanBatchType.ALL, [normal_id, marker_id])
with app.state.session_factory() as session:
    persisted = session.get(ScanBatch, batch.id)
    assert [item.device_id for item in persisted.items] == [normal_id]
```

Import `DeviceCollectionDisabled` and `PRIORITY_MANUAL` from `app.services.scan_queue`.

- [ ] **Step 2: Add failing API and final-executor tests**

Add to `tests/test_scan_queue_api.py`:

```python
def test_marker_manual_scan_returns_conflict(client, app):
    marker_id = seed_marker_device(app, host="10.0.1.90")
    response = client.post(f"/api/devices/{marker_id}/scan")
    assert response.status_code == 409
    assert response.json()["detail"] == "该设备仅用于集群标注，未配置采集凭据"


def test_all_and_cluster_batches_exclude_markers(client, app):
    cluster_id, normal_id, marker_id = seed_collectible_and_marker_cluster(app)
    all_batch = client.post("/api/scan-batches", json={"scope": "all"}).json()
    cluster_batch = client.post(
        "/api/scan-batches",
        json={"scope": "cluster", "cluster_id": cluster_id},
    ).json()
    assert all_batch["total_tasks"] == 1
    assert cluster_batch["total_tasks"] == 1
    assert normal_id != marker_id
```

Implement the test helpers locally using `Device`, `Cluster`, `OSType`, and `app.state.cipher.encrypt("")`; do not call the create-device API because it intentionally cannot create markers.

Add to `tests/test_services.py`:

```python
def test_scan_service_refuses_marker_before_collector(app):
    collector = RecordingCollector()
    with app.state.session_factory() as session:
        marker = Device(
            name="marker",
            host="10.0.0.91",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(""),
            collection_enabled=False,
        )
        session.add(marker)
        session.commit()
        with pytest.raises(CollectionDisabled, match="仅用于集群标注"):
            ScanService(
                session,
                app.state.cipher,
                linux_collector=collector,
                windows_collector=collector,
            ).run(marker.id, ScanTrigger.MANUAL)
        assert session.scalar(
            select(func.count()).select_from(ScanRun).where(
                ScanRun.device_id == marker.id
            )
        ) == 0
    assert collector.seen_devices == []
```

Add the required `pytest`, `func`, `ScanRun`, and `CollectionDisabled` imports.

- [ ] **Step 3: Run scan-guard tests and verify failure**

Run: `pytest tests/test_scheduler_queue.py tests/test_scan_queue.py tests/test_scan_queue_api.py tests/test_services.py -k "marker or batches_exclude" -v`

Expected: FAIL because marker devices are still scheduled, queued, and executed.

- [ ] **Step 4: Implement scheduler, queue, API, and executor guards**

In `app/services/scheduler.py`, make both synchronization and startup queries capability-aware:

```python
if not device.collection_enabled or not device.scheduled_enabled:
    if self.scheduler.get_job(job_id):
        self.scheduler.remove_job(job_id)
    return
```

```python
select(Device).where(
    Device.scheduled_enabled.is_(True),
    Device.collection_enabled.is_(True),
)
```

In `app/services/scan_queue.py`, define:

```python
COLLECTION_DISABLED_MESSAGE = "该设备仅用于集群标注，未配置采集凭据"


class DeviceCollectionDisabled(RuntimeError):
    pass
```

At the start of `_enqueue_in_session`:

```python
device = session.get(Device, device_id)
if device is None:
    raise ValueError("设备不存在")
if not device.collection_enabled:
    raise DeviceCollectionDisabled(COLLECTION_DISABLED_MESSAGE)
```

Filter batch candidates in `_create_batch_in_session`:

```python
existing_devices = set(
    session.scalars(
        select(Device.id).where(
            Device.id.in_(unique_ids),
            Device.collection_enabled.is_(True),
        )
    ).all()
)
```

In `app/services/scans.py`, define and enforce the final guard before creating `ScanRun`:

```python
class CollectionDisabled(RuntimeError):
    pass
```

```python
if not device.collection_enabled:
    raise CollectionDisabled("该设备仅用于集群标注，未配置采集凭据")
```

In `app/routes/api.py`, import the queue exception/constant, check the loaded device in `run_scan`, and map the queue exception too:

```python
device = db.get(Device, device_id)
if device is None:
    raise HTTPException(status_code=404, detail="设备不存在")
if not device.collection_enabled:
    raise HTTPException(status_code=409, detail=COLLECTION_DISABLED_MESSAGE)
```

```python
except DeviceCollectionDisabled as exc:
    raise HTTPException(status_code=409, detail=str(exc)) from exc
```

Also begin both all-device and cluster batch statements with:

```python
statement = select(Device.id).where(Device.collection_enabled.is_(True))
```

- [ ] **Step 5: Run all scan, scheduler, and API tests**

Run: `pytest tests/test_scheduler_queue.py tests/test_scan_queue.py tests/test_scan_queue_api.py tests/test_services.py tests/test_api.py -v`

Expected: PASS; a marker produces no job, task, batch item, `ScanRun`, or collector call.

- [ ] **Step 6: Commit scan enforcement**

```bash
git add app/services/scheduler.py app/services/scan_queue.py app/services/scans.py app/routes/api.py tests/test_scheduler_queue.py tests/test_scan_queue.py tests/test_scan_queue_api.py tests/test_services.py
git commit -m "feat: exclude cluster markers from collection"
```

### Task 4: Upgrade a marker only after a successful credential test

**Files:**
- Modify: `app/routes/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: existing `PUT /api/devices/{device_id}` and `ScanService.test_connection(...)`.
- Produces: a successful non-empty `password` update sets `collection_enabled=true` atomically with the encrypted password.
- Guarantees: failed connection test and password-free metadata updates preserve `collection_enabled=false`.

- [ ] **Step 1: Add failing success/failure/metadata upgrade tests**

Add a local `seed_marker_device(app)` helper in `tests/test_api.py`, then add:

```python
def test_valid_password_update_enables_marker_collection(client, app):
    marker_id = seed_marker_device(app, host="10.0.2.10")
    response = client.put(
        f"/api/devices/{marker_id}",
        json={"password": "working-password"},
    )
    assert response.status_code == 200
    assert response.json()["collection_enabled"] is True
    with app.state.session_factory() as session:
        device = session.get(Device, marker_id)
        assert device.collection_enabled is True
        assert app.state.cipher.decrypt(device.encrypted_password) == "working-password"


def test_failed_password_update_leaves_marker_disabled(client, app, monkeypatch):
    marker_id = seed_marker_device(app, host="10.0.2.11")
    app.state.linux_collector.fail = CollectorError(
        "authentication_failed",
        "认证失败",
    )
    response = client.put(
        f"/api/devices/{marker_id}",
        json={"password": "bad-password"},
    )
    assert response.status_code == 502
    with app.state.session_factory() as session:
        device = session.get(Device, marker_id)
        assert device.collection_enabled is False
        assert app.state.cipher.decrypt(device.encrypted_password) == ""


def test_metadata_update_does_not_enable_marker(client, app):
    marker_id = seed_marker_device(app, host="10.0.2.12")
    response = client.put(
        f"/api/devices/{marker_id}",
        json={"name": "renamed-marker"},
    )
    assert response.status_code == 200
    assert response.json()["collection_enabled"] is False
```

Import `CollectorError` from `app.collectors.base` and `Device`, `OSType` from `app.models`. The local marker helper must create a `Device` with `encrypted_password=app.state.cipher.encrypt("")` and `collection_enabled=False`, commit it, and return its ID.

- [ ] **Step 2: Run upgrade tests and verify failure**

Run: `pytest tests/test_api.py -k "marker_collection or marker_disabled or metadata_update" -v`

Expected: the successful password case FAILS because the flag remains false; the preservation tests should already pass or expose accidental mutation.

- [ ] **Step 3: Enable collection in the already-tested password branch**

In `update_device`, immediately beside the existing password assignment, use:

```python
if password:
    device.encrypted_password = request.app.state.cipher.encrypt(password)
    device.collection_enabled = True
```

Do not set the flag before `_scan_service(...).test_connection(...)`; this preserves the old encrypted empty password and false flag on HTTP 502.

- [ ] **Step 4: Run API and scheduler regressions**

Run: `pytest tests/test_api.py tests/test_scheduler_queue.py -v`

Expected: PASS; a successful update response is collectible and scheduler synchronization can add its job, while failure/metadata updates remain markers.

- [ ] **Step 5: Commit credential upgrade behavior**

```bash
git add app/routes/api.py tests/test_api.py
git commit -m "feat: enable markers after credential verification"
```

### Task 5: Mark cluster-only devices in the UI and verify topology membership

**Files:**
- Modify: `app/templates/devices.html`
- Test: `tests/test_device_list_frontend.py`
- Test: `tests/test_cluster_topology.py`

**Interfaces:**
- Consumes: `Device.collection_enabled` and existing cluster membership topology logic.
- Produces: visible text `仅标注` and a disabled action titled `未配置采集凭据` for marker rows.
- Guarantees: cluster node member data includes marker devices, while edges still come only from recorded scans.

- [ ] **Step 1: Add failing template assertions**

Add to `tests/test_device_list_frontend.py`:

```python
def test_marker_device_is_labeled_and_scan_is_disabled():
    template = Path("app/templates/devices.html").read_text(encoding="utf-8")
    assert "{% if not device.collection_enabled %}" in template
    assert "仅标注" in template
    assert 'title="未配置采集凭据"' in template
    assert "{% else %}" in template
    assert 'data-scan="{{ device.id }}"' in template
```

- [ ] **Step 2: Add a failing topology membership test**

In `tests/test_cluster_topology.py`, add:

```python
def test_cluster_topology_includes_marker_member_without_fake_edges(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="marker-cluster")
        session.add(cluster)
        session.flush()
        marker = add_device(
            session,
            app,
            "marker-only",
            "10.10.10.10",
            cluster,
        )
        marker.collection_enabled = False
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())

        node = next(
            node for node in topology["nodes"]
            if node["data"]["id"] == f"cluster-{cluster.id}"
        )
        assert [member["id"] for member in node["data"]["members"]] == [marker.id]
        assert topology["edges"] == []
```

- [ ] **Step 3: Run both focused tests and verify UI failure**

Run: `pytest tests/test_device_list_frontend.py tests/test_cluster_topology.py -k "marker" -v`

Expected: template test FAILS until the marker branch is rendered; topology test should pass and locks in the existing membership behavior.

- [ ] **Step 4: Render marker label and a non-action scan control**

In the device identity cell, append:

```html
{% if not device.collection_enabled %}
<small class="cluster-managed-hint">仅标注</small>
{% endif %}
```

Replace the immediate scan button with:

```html
{% if not device.collection_enabled %}
<button class="icon-button" type="button" disabled title="未配置采集凭据">↻</button>
{% else %}
<button data-scan="{{ device.id }}" class="icon-button" type="button" title="立即采集">↻</button>
{% endif %}
```

- [ ] **Step 5: Run frontend/topology regressions**

Run: `pytest tests/test_device_list_frontend.py tests/test_cluster_topology.py tests/test_pages.py -v`

Expected: PASS; marker is listed as a cluster member and has no clickable scan action.

- [ ] **Step 6: Commit UI and topology coverage**

```bash
git add app/templates/devices.html tests/test_device_list_frontend.py tests/test_cluster_topology.py
git commit -m "feat: label cluster-only marker devices"
```

### Task 6: Full regression and design-contract verification

**Files:**
- Verify: `docs/superpowers/specs/2026-08-04-passwordless-cluster-membership-import-design.md`
- Verify: all modified application and test files

**Interfaces:**
- Verifies all Task 1–5 interfaces together without adding another production abstraction.

- [ ] **Step 1: Run formatting/lint checks configured by the repository**

Run: `ruff check app tests`

Expected: PASS with no lint errors. If `ruff` is not installed or not configured, record that exact command failure and continue to pytest; do not introduce a new dependency.

- [ ] **Step 2: Run the complete automated suite**

Run: `pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Verify schema and request-boundary contracts**

Run:

```bash
python -c "from app.migrations import LATEST_SCHEMA_VERSION; from app.schemas import DeviceCreate, DeviceUpdate, DeviceRead; assert LATEST_SCHEMA_VERSION == 9; assert 'collection_enabled' not in DeviceCreate.model_fields; assert 'collection_enabled' not in DeviceUpdate.model_fields; assert 'collection_enabled' in DeviceRead.model_fields; print('contracts ok')"
```

Expected: `contracts ok`.

- [ ] **Step 4: Search for accidental credential logging or unguarded batch selection**

Run: `rg -n "password|encrypted_password|select\(Device.id\)|collection_enabled" app/services/imports.py app/services/scheduler.py app/services/scan_queue.py app/services/scans.py app/routes/api.py`

Expected: the duplicate warning has identity/context fields only; scheduler, queue, executor, and API batch selection each reference `collection_enabled`; no log statement includes password variables.

- [ ] **Step 5: Commit any test-only corrections from the verification pass**

If verification required tracked corrections, commit only those files:

```bash
git add app tests
git commit -m "test: complete passwordless marker regressions"
```

If `git status --short` is empty, do not create an empty commit.
