# Cluster Excel Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This environment executes the same checklist inline because that sub-skill is unavailable.

**Goal:** Add managed clusters, secure Excel device import with persistent validation results, and a cluster topology that hides intra-cluster links.

**Architecture:** Extend the existing FastAPI/SQLAlchemy monolith with versioned SQLite migrations, normalized cluster and import entities, an `openpyxl` import service, a bounded background test queue, and a new all-device topology aggregation endpoint. Preserve the current single-device topology API and page mode.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy 2.x, APScheduler, openpyxl, Jinja2, Cytoscape.js, pytest

## Global Constraints

- Existing SQLite devices, encrypted credentials, scan runs, and connection records must survive migration.
- Uploaded source workbooks are never persisted.
- Passwords are encrypted immediately and never appear in reports, logs, or API responses.
- Import accepts `.xlsx` files up to 5 MB and 1000 nonblank data rows.
- Duplicate identity is `(host, port, username)` and is skipped.
- Background connection tests run with at most three workers and resume pending rows after restart.
- Cluster topology uses each device’s latest successful scan and hides links whose source and managed target share the same non-null cluster.
- Existing device topology behavior remains unchanged.

---

### Task 1: Schema Migration and Cluster Models

**Files:**
- Create: `app/migrations.py`
- Modify: `app/models.py`
- Modify: `app/database.py`
- Modify: `app/main.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: `run_migrations(engine)`, `Cluster`, `ImportBatch`, `ImportRowResult`, and nullable `Device.cluster_id`.

- [ ] Write a test that creates the previous `devices` schema, inserts one device, runs `run_migrations`, and asserts the row remains while `cluster_id`, `clusters`, `import_batches`, and `import_row_results` exist.
- [ ] Implement idempotent schema version 2 migration using SQLAlchemy inspection and transaction-scoped DDL.
- [ ] Add ORM relationships with cluster deletion handled by the service layer.
- [ ] Call migration before `Base.metadata.create_all` and scheduler startup.
- [ ] Run `pytest tests/test_migrations.py -q`; expect all migration tests to pass.

### Task 2: Cluster CRUD and Device Integration

**Files:**
- Modify: `app/schemas.py`
- Create: `app/services/clusters.py`
- Modify: `app/routes/api.py`
- Modify: `app/templates/devices.html`
- Test: `tests/test_clusters.py`

**Interfaces:**
- Produces: `resolve_cluster(session, cluster_id, new_cluster_name) -> Cluster | None`.
- Produces: `GET/POST /api/clusters`, `PUT/DELETE /api/clusters/{id}`.
- Extends device create/update/read schemas with `cluster_id`, `cluster_name`, and `new_cluster_name`.

- [ ] Write tests for create, case-insensitive duplicate rejection, rename, delete-to-unassigned, select existing cluster, and quick-create cluster.
- [ ] Implement normalized cluster lookup and the mutually exclusive `cluster_id`/`new_cluster_name` rule.
- [ ] Add cluster fields to device APIs without exposing credentials.
- [ ] Add cluster selection and quick-create controls to manual device creation.
- [ ] Run `pytest tests/test_clusters.py tests/test_api.py -q`; expect all tests to pass.

### Task 3: Excel Template, Import, and Report

**Files:**
- Modify: `pyproject.toml`
- Create: `app/services/imports.py`
- Modify: `app/routes/api.py`
- Test: `tests/test_imports.py`

**Interfaces:**
- Produces: `build_import_template() -> bytes`, `import_devices(session, cipher, filename, content) -> ImportBatch`, and `build_import_report(batch) -> bytes`.
- Produces: `GET /api/imports/template`, `POST /api/imports`, `GET /api/imports/{id}`, `GET /api/imports/{id}/rows`, and `GET /api/imports/{id}/report`.

- [ ] Add `openpyxl>=3.1,<4`.
- [ ] Write tests for exact headers, default ports, mixed valid/error/duplicate rows, cluster auto-creation, encrypted passwords, file limits, and password-free reports.
- [ ] Generate a styled template with `设备导入` and `填写说明` sheets.
- [ ] Parse workbook bytes with `load_workbook(BytesIO(content), read_only=True, data_only=True)`.
- [ ] Process each normalized row with an isolated savepoint; persist only sanitized row results.
- [ ] Generate a result workbook without a password column.
- [ ] Run `pytest tests/test_imports.py -q`; expect all tests to pass.

### Task 4: Persistent Background Connection Tests

**Files:**
- Create: `app/services/import_testing.py`
- Modify: `app/services/scheduler.py`
- Modify: `app/routes/api.py`
- Test: `tests/test_import_testing.py`

**Interfaces:**
- Produces: `ImportTestService.schedule_batch(batch_id)`, `resume_pending()`, and `test_row(row_id)`.

- [ ] Write tests that use fake collectors to verify success/failure counts, retention of failed devices, pending-row resume, and a maximum of three active jobs.
- [ ] Add a three-worker executor and stable job IDs `import-test-{row_id}`.
- [ ] Decrypt each imported device credential only inside its test job.
- [ ] Persist every row completion immediately and finalize batch counters when no pending rows remain.
- [ ] Resume pending rows at application startup.
- [ ] Run `pytest tests/test_import_testing.py -q`; expect all tests to pass.

### Task 5: Cluster Topology Aggregation

**Files:**
- Modify: `app/services/topology.py`
- Modify: `app/routes/api.py`
- Test: `tests/test_cluster_topology.py`

**Interfaces:**
- Produces: `build_cluster_topology(session, resolver) -> dict`.
- Produces: `GET /api/topology/clusters`.

- [ ] Write fixtures with same-cluster, cross-cluster, ungrouped, external, listener-only, and no-snapshot devices.
- [ ] Add a ten-minute hostname resolver cache and ambiguous-address warnings.
- [ ] Build one address-to-device index from host literals and resolved addresses.
- [ ] Read the latest successful scan for each device, group source and managed target nodes, omit same-cluster/self edges, and aggregate directed edges.
- [ ] Preserve source device, target device/IP, connection, scan ID, and scan time in edge detail records.
- [ ] Run `pytest tests/test_cluster_topology.py tests/test_api.py -q`; expect all tests to pass.

### Task 6: Import and Cluster Topology Interface

**Files:**
- Modify: `app/templates/devices.html`
- Modify: `app/templates/topology.html`
- Modify: `app/static/js/topology.js`
- Modify: `app/static/css/app.css`
- Modify: `app/routes/pages.py`
- Test: `tests/test_pages.py`

**Interfaces:**
- Adds a batch import panel and persistent result drawer to `/devices`.
- Adds device/cluster mode switching to `/topology`.

- [ ] Add page tests for the import trigger, template link, cluster controls, and topology mode selector.
- [ ] Add secure source-file warning, upload progress, batch counters, row result table, and report download.
- [ ] Add the approved top segmented mode control.
- [ ] Keep existing device mode logic; cluster mode loads `/api/topology/clusters` and renders cluster/device/external nodes distinctly.
- [ ] Show members and scan freshness for cluster nodes and constituent connections for aggregate edges.
- [ ] Run `pytest tests/test_pages.py -q`; expect all tests to pass.

### Task 7: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `.env.example` only if new configuration is introduced.

**Interfaces:**
- Documents the Excel schema, plaintext-password warning, cluster rules, migration, and cluster topology semantics.

- [ ] Update operator documentation and usage flow.
- [ ] Run `python -m ruff check app tests`; expect success.
- [ ] Run `python -m pytest --cov=app --cov-report=term-missing`; expect all tests to pass.
- [ ] Restart the local service, verify the template downloads as a valid workbook, and verify `/api/topology/clusters` returns JSON.
- [ ] Verify no uploaded workbook, plaintext password, generated report, or database file is tracked.
