# Scoped Cluster Topology Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make selected-cluster topology requests query only direct outbound and inbound connection candidates before aggregation, so current and historical views do not process unrelated connection records.

**Architecture:** Add a reusable SQL candidate-ID builder that expresses outbound and inbound paths as a `UNION`, backed by both scan-first and remote-IP-first indexes. Refactor current aggregation to accept queried connection observations instead of requiring fully loaded scan relationships, reuse the candidate builder in historical aggregation, and let `build_cluster_topology()` pass target scope before either aggregation path runs.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy 2.x, SQLite, pytest, Ruff

## Global Constraints

- Keep the `/api/topology/clusters` response structure unchanged.
- Preserve target-cluster outbound connections, inbound connections, one-hop cropping, same-cluster hiding, internal-CIDR hiding, IPv4-mapped normalization, and duplicate-address behavior.
- Keep the frontend timeout at 15 seconds.
- Do not add a persistent summary table, third-party dependency, or historical backfill.
- Do not change collection, scan queueing, cache invalidation, or history retention behavior.
- Keep unscoped internal cluster-topology calls working.

---

### Task 1: Add the Reverse Inbound Lookup Index

**Files:**
- Modify: `app/models.py:299-310`
- Modify: `app/migrations.py:3,91-101,157-164`
- Test: `tests/test_migrations.py:35-67`

**Interfaces:**
- Consumes: existing `connection_records.remote_ip` and `connection_records.scan_run_id` columns.
- Produces: `ix_connection_remote_scan(remote_ip, scan_run_id)` in new and upgraded SQLite databases.
- Produces: schema version `8`.

- [ ] **Step 1: Write the failing migration assertion**

In `tests/test_migrations.py`, extend `test_cluster_internal_network_table_and_indexes_are_created()`:

```python
    connection_indexes = {
        index["name"] for index in inspector.get_indexes("connection_records")
    }
    assert "ix_connection_history_service" in connection_indexes
    assert "ix_connection_remote_scan" in connection_indexes
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT MAX(version) FROM schema_versions")
        ).scalar() == 8
```

- [ ] **Step 2: Run the migration test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py::test_cluster_internal_network_table_and_indexes_are_created -v
```

Expected: failure because `ix_connection_remote_scan` is absent and the latest version is `7`.

- [ ] **Step 3: Declare and migrate the index**

Add this index to `ConnectionRecord.__table_args__` in `app/models.py`:

```python
        Index(
            "ix_connection_remote_scan",
            "remote_ip",
            "scan_run_id",
        ),
```

Update and extend `app/migrations.py`:

```python
LATEST_SCHEMA_VERSION = 8
```

```python
        if "connection_records" in inspector.get_table_names():
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_connection_history_service "
                    "ON connection_records "
                    "(scan_run_id, remote_ip, remote_port, protocol, process_name)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_connection_remote_scan "
                    "ON connection_records (remote_ip, scan_run_id)"
                )
            )
```

Keep the existing final `INSERT OR IGNORE INTO schema_versions` call; it will now record version `8`.

- [ ] **Step 4: Run migration tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py -q
```

Expected: all migration tests pass.

- [ ] **Step 5: Commit the index migration**

```powershell
git add app/models.py app/migrations.py tests/test_migrations.py
git commit -m "perf: index inbound topology lookups"
```

---

### Task 2: Build Union-Scoped Candidate Queries and Current Aggregation

**Files:**
- Modify: `app/services/topology_history.py:1-210`
- Test: `tests/test_topology_history.py`

**Interfaces:**
- Produces: `_candidate_connection_ids(scan_conditions, source_device_ids=None, inbound_addresses=None)` returning a SQLAlchemy `Select` or `CompoundSelect` of connection IDs.
- Produces: `aggregate_current_connections(session, current_scans, source_device_ids=None, inbound_addresses=None) -> list[dict]`.
- Preserves: `aggregate_service_connections(scans, current_scan_ids) -> list[dict]` and its output fields.

- [ ] **Step 1: Write failing current-scope tests**

Import the new function in `tests/test_topology_history.py`:

```python
from app.services.topology_history import aggregate_current_connections
```

Add a test that includes outbound, inbound, unrelated, and duplicate-path records:

```python
def test_current_sql_filters_to_outbound_and_inbound_candidates(app):
    with app.state.session_factory() as session:
        selected = add_device(session, app, "selected-current", "10.0.0.10")
        inbound = add_device(session, app, "inbound-current", "10.0.0.20")
        unrelated = add_device(session, app, "unrelated-current", "10.0.0.30")
        selected_scan = add_scan(
            session,
            selected,
            started_at=NOW,
            remote_ip=selected.host,
        )
        inbound_scan = add_scan(
            session,
            inbound,
            started_at=NOW,
            remote_ip=selected.host,
        )
        unrelated_scan = add_scan(
            session,
            unrelated,
            started_at=NOW,
            remote_ip="198.51.100.9",
        )
        session.commit()
        latest = {
            selected.id: selected_scan,
            inbound.id: inbound_scan,
            unrelated.id: unrelated_scan,
        }

        rows = aggregate_current_connections(
            session,
            latest,
            source_device_ids={selected.id},
            inbound_addresses={selected.host},
        )

        assert {
            (row["source_device_id"], row["remote_ip"])
            for row in rows
        } == {
            (selected.id, selected.host),
            (inbound.id, selected.host),
        }
```

Add an equivalence test for unscoped current aggregation:

```python
def test_current_sql_matches_existing_python_aggregation(app):
    with app.state.session_factory() as session:
        device = add_device(session, app, "current-equivalent", "10.0.0.40")
        current = add_scan(
            session,
            device,
            started_at=NOW,
            remote_ip="::ffff:203.0.113.8",
        )
        session.commit()

        expected = aggregate_service_connections([current], {current.id})
        actual = aggregate_current_connections(session, {device.id: current})

        assert service_projection(actual) == service_projection(expected)
```

- [ ] **Step 2: Run the focused tests and verify the import fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -k "current_sql" -v
```

Expected: collection fails because `aggregate_current_connections` does not exist.

- [ ] **Step 3: Extract observation aggregation from loaded scans**

In `app/services/topology_history.py`, import `Iterable` and add an observation alias:

```python
from collections.abc import Collection, Iterable, Sequence

ConnectionObservation = tuple[int, int, datetime, ConnectionRecord]
```

Move the existing grouping body of `aggregate_service_connections()` into this helper, replacing direct `scan` references with tuple fields:

```python
def _aggregate_connection_observations(
    observations: Iterable[ConnectionObservation],
    current_scan_ids: Collection[int],
) -> list[dict]:
    from app.services.topology import connection_dict

    current_ids = set(current_scan_ids)
    groups: dict[tuple, dict] = {}
    for scan_id, device_id, started_at, row in sorted(
        observations,
        key=lambda item: (item[2], item[0], item[3].id),
    ):
        if row.remote_ip is None or is_loopback_address(row.remote_ip):
            continue
        key = _service_key(device_id, row)
        normalized_remote = key[2]
        if normalized_remote is None:
            continue
        values = connection_dict(row)
        values["remote_ip"] = normalized_remote
        bucket = groups.get(key)
        if bucket is None:
            bucket = {
                **values,
                "source_device_id": device_id,
                "scan_id": scan_id,
                "scan_time": started_at.isoformat(),
                "is_current": False,
                "first_seen": started_at.isoformat(),
                "last_seen": started_at.isoformat(),
                "observation_count": 0,
                "_scan_ids": set(),
                "_local_ips": set(),
                "_local_ports": set(),
                "_pids": set(),
            }
            groups[key] = bucket
        else:
            old_hostname = bucket.get("remote_hostname")
            bucket.update(values)
            if not bucket.get("remote_hostname"):
                bucket["remote_hostname"] = old_hostname
            bucket["scan_id"] = scan_id
            bucket["scan_time"] = started_at.isoformat()
            bucket["last_seen"] = started_at.isoformat()
        bucket["is_current"] = bucket["is_current"] or scan_id in current_ids
        bucket["_local_ips"].add(values["local_ip"])
        bucket["_local_ports"].add(values["local_port"])
        if values["pid"] is not None:
            bucket["_pids"].add(values["pid"])
        bucket["_scan_ids"].add(scan_id)

    result = []
    for key in sorted(groups, key=str):
        bucket = groups[key]
        bucket["observation_count"] = len(bucket.pop("_scan_ids"))
        bucket["observed_local_ips"] = sorted(bucket.pop("_local_ips"))
        bucket["observed_local_ports"] = sorted(bucket.pop("_local_ports"))
        bucket["observed_pids"] = sorted(bucket.pop("_pids"))
        result.append(bucket)
    return result
```

Keep the public function as an adapter:

```python
def aggregate_service_connections(
    scans: Sequence[ScanRun],
    current_scan_ids: Collection[int],
) -> list[dict]:
    observations = (
        (scan.id, scan.device_id, scan.started_at, row)
        for scan in scans
        for row in scan.connections
    )
    return _aggregate_connection_observations(observations, current_scan_ids)
```

- [ ] **Step 4: Add the reusable union candidate builder and current query**

Add these functions after `_service_key()`:

```python
def _candidate_connection_ids(
    scan_conditions,
    *,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
):
    base = (
        select(ConnectionRecord.id)
        .join(ScanRun, ScanRun.id == ConnectionRecord.scan_run_id)
        .where(*scan_conditions, ConnectionRecord.remote_ip.is_not(None))
    )
    if source_device_ids is None and inbound_addresses is None:
        return base
    sources = sorted(set(source_device_ids or ()))
    addresses = sorted(set(inbound_addresses or ()))
    outbound = base.where(ScanRun.device_id.in_(sources))
    inbound = base.where(ConnectionRecord.remote_ip.in_(addresses))
    return outbound.union(inbound)


def aggregate_current_connections(
    session: Session,
    current_scans: dict[int, ScanRun],
    *,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
) -> list[dict]:
    current_ids = {scan.id for scan in current_scans.values()}
    if not current_ids:
        return []
    candidate_ids = _candidate_connection_ids(
        [ScanRun.id.in_(current_ids)],
        source_device_ids=source_device_ids,
        inbound_addresses=inbound_addresses,
    )
    rows = session.execute(
        select(
            ConnectionRecord,
            ScanRun.id.label("scan_id"),
            ScanRun.device_id.label("device_id"),
            ScanRun.started_at.label("started_at"),
        )
        .join(ScanRun, ScanRun.id == ConnectionRecord.scan_run_id)
        .where(ConnectionRecord.id.in_(candidate_ids))
        .order_by(ScanRun.started_at, ScanRun.id, ConnectionRecord.id)
    ).all()
    observations = (
        (scan_id, device_id, started_at, connection)
        for connection, scan_id, device_id, started_at in rows
    )
    return _aggregate_connection_observations(observations, current_ids)
```

- [ ] **Step 5: Run topology-history tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -q
```

Expected: all topology-history tests pass, including scoped current equivalence.

- [ ] **Step 6: Commit current scoped aggregation**

```powershell
git add app/services/topology_history.py tests/test_topology_history.py
git commit -m "perf: scope current topology connections"
```

---

### Task 3: Reuse Union Candidates in Historical Aggregation

**Files:**
- Modify: `app/services/topology_history.py:210-350`
- Test: `tests/test_topology_history.py:360-430`

**Interfaces:**
- Consumes: `_candidate_connection_ids()` from Task 2.
- Preserves: `aggregate_historical_connections()` signature and exact aggregation fields.

- [ ] **Step 1: Add a regression that proves overlap is deduplicated**

Extend `tests/test_topology_history.py`:

```python
def test_sql_history_union_deduplicates_connection_matching_both_paths(app):
    with app.state.session_factory() as session:
        selected = add_device(session, app, "selected-overlap", "10.0.0.50")
        current = add_scan(
            session,
            selected,
            started_at=NOW,
            remote_ip=selected.host,
        )
        session.commit()

        rows = aggregate_historical_connections(
            session,
            [selected.id],
            {current.id},
            "1d",
            now=NOW,
            source_device_ids={selected.id},
            inbound_addresses={selected.host},
        )

        assert len(rows) == 1
        assert rows[0]["observation_count"] == 1
```

- [ ] **Step 2: Run the scoped historical tests before refactoring**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -k "filters_candidates or union_deduplicates" -v
```

Expected: tests pass, establishing behavior that must survive the query rewrite.

- [ ] **Step 3: Replace the historical OR filter with candidate-ID UNION**

In `aggregate_historical_connections()`, keep the existing scan conditions but remove this block:

```python
    if source_device_ids is not None or inbound_addresses is not None:
        sources = sorted(set(source_device_ids or ()))
        addresses = sorted(set(inbound_addresses or ()))
        conditions.append(
            or_(
                ScanRun.device_id.in_(sources),
                ConnectionRecord.remote_ip.in_(addresses),
            )
        )
```

Build candidate IDs instead:

```python
    candidate_ids = _candidate_connection_ids(
        conditions,
        source_device_ids=source_device_ids,
        inbound_addresses=inbound_addresses,
    )
```

Change the `eligible` subquery filter so its outer query reads only union-selected IDs:

```python
        .join(ScanRun, ScanRun.id == ConnectionRecord.scan_run_id)
        .where(ConnectionRecord.id.in_(candidate_ids))
        .subquery()
```

The candidate query already applies all status, time, current-baseline, device, and non-null-remote conditions, so do not repeat `conditions` in the outer query.

- [ ] **Step 4: Run historical equivalence and scope tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -v
```

Expected: all historical and current aggregation tests pass.

- [ ] **Step 5: Commit the historical query rewrite**

```powershell
git add app/services/topology_history.py tests/test_topology_history.py
git commit -m "perf: split historical cluster candidate paths"
```

---

### Task 4: Apply Scope Before Cluster Topology Aggregation

**Files:**
- Modify: `app/services/topology.py:1-20,235-315`
- Test: `tests/test_cluster_topology.py:117-175`

**Interfaces:**
- Consumes: `aggregate_current_connections()` from Task 2.
- Produces: scoped services before the node/edge construction loop for every target-cluster window.
- Preserves: `build_cluster_topology()` signature and result schema.

- [ ] **Step 1: Add current inbound and unrelated-volume semantics**

Replace `test_cluster_topology_returns_only_target_one_hop()` data setup so it includes a direct inbound peer and many unrelated connections, then assert exact visible pairs:

```python
def test_current_target_cluster_keeps_only_outbound_and_inbound_one_hop(app):
    with app.state.session_factory() as session:
        selected = Cluster(name="目标集群")
        peer = Cluster(name="对端集群")
        unrelated = Cluster(name="无关集群")
        session.add_all([selected, peer, unrelated])
        session.flush()
        source = add_device(session, app, "source", "10.0.0.1", selected)
        inbound = add_device(session, app, "inbound", "10.0.1.1", peer)
        other = add_device(session, app, "other", "10.0.2.1", unrelated)
        add_scan(session, source, [inbound.host, "203.0.113.8"])
        add_scan(session, inbound, [source.host])
        add_scan(
            session,
            other,
            [f"198.51.100.{index}" for index in range(1, 201)],
        )
        session.commit()

        topology = build_cluster_topology(
            session,
            LiteralResolver(),
            target_cluster_id=selected.id,
        )

        assert {
            (edge["data"]["source"], edge["data"]["target"])
            for edge in topology["edges"]
        } == {
            (f"cluster-{selected.id}", f"cluster-{peer.id}"),
            (f"cluster-{selected.id}", "external-203.0.113.8"),
            (f"cluster-{peer.id}", f"cluster-{selected.id}"),
        }
        assert not any(
            detail["source_device_id"] == other.id
            for edge in topology["edges"]
            for detail in edge["data"]["connections"]
        )
```

- [ ] **Step 2: Run the focused test before integration**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py -k "current_target_cluster" -v
```

Expected: the semantics pass on the old implementation, establishing an integration regression baseline.

- [ ] **Step 3: Load scan metadata only and pass the target scope to both aggregators**

Import `aggregate_current_connections` in `app/services/topology.py`. Replace the current `latest_scans` and service-selection block with:

```python
    latest_scans = load_current_scans(
        session,
        device_ids,
        with_connections=target_cluster_id is None and window == "current",
    )
    current_scan_ids = {scan.id for scan in latest_scans.values()}
    source_device_ids = None
    inbound_addresses = None
    if target_cluster_id is not None:
        members = cluster_members.get(target_cluster_id, [])
        source_device_ids = {device.id for device in members}
        normalized_addresses = {
            address
            for device in members
            for address in resolver.resolve(device.host)
        }
        inbound_addresses = set(normalized_addresses)
        inbound_addresses.update(
            f"::ffff:{address}"
            for address in normalized_addresses
            if ":" not in address
        )
    if window == "current":
        services = (
            aggregate_service_connections(
                list(latest_scans.values()),
                current_scan_ids,
            )
            if target_cluster_id is None
            else aggregate_current_connections(
                session,
                latest_scans,
                source_device_ids=source_device_ids,
                inbound_addresses=inbound_addresses,
            )
        )
    else:
        services = aggregate_historical_connections(
            session,
            device_ids,
            current_scan_ids,
            window,
            now=now,
            source_device_ids=source_device_ids,
            inbound_addresses=inbound_addresses,
        )
```

Delete the duplicated historical-only target-scope calculation that this block replaces.

- [ ] **Step 4: Run cluster and API regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py tests/test_topology_history.py tests/test_api.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the early-scope integration**

```powershell
git add app/services/topology.py tests/test_cluster_topology.py
git commit -m "perf: scope selected cluster before aggregation"
```

---

### Task 5: Add Scoped Performance Coverage and Complete Regression

**Files:**
- Modify: `scripts/benchmark_topology_history.py`
- Modify: `tests/test_topology_history_benchmark.py`

**Interfaces:**
- Consumes: scoped current and historical aggregators from Tasks 2-4.
- Produces: a benchmark result containing `scoped_seconds`, `scoped_groups`, and `scoped_within_target`.

- [ ] **Step 1: Extend the smoke benchmark assertion**

Update `tests/test_topology_history_benchmark.py`:

```python
def test_history_benchmark_smoke(tmp_path):
    result = run_benchmark(
        rows=5_000,
        max_seconds=10,
        database_path=tmp_path / "benchmark.db",
    )

    assert result["raw_rows"] == 5_000
    assert result["service_groups"] > 0
    assert result["within_target"] is True
    assert result["scoped_groups"] > 0
    assert result["scoped_groups"] <= result["service_groups"]
    assert result["scoped_within_target"] is True
```

- [ ] **Step 2: Run the benchmark smoke test and verify the new fields fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history_benchmark.py -v
```

Expected: failure with missing `scoped_groups`.

- [ ] **Step 3: Measure a representative selected-device scope**

In `run_benchmark()`, after the existing unscoped measurement, add:

```python
            scoped_started_at = time.perf_counter()
            scoped_services = aggregate_historical_connections(
                session,
                devices,
                current_scan_ids,
                "1d",
                now=reference,
                source_device_ids={devices[0]},
                inbound_addresses={f"10.10.0.{devices[0]}"},
            )
            scoped_seconds = time.perf_counter() - scoped_started_at
            scoped_groups = len(scoped_services)
```

Add these fields to the returned dictionary:

```python
            "scoped_groups": scoped_groups,
            "scoped_seconds": round(scoped_seconds, 3),
            "scoped_within_target": scoped_seconds <= max_seconds,
```

- [ ] **Step 4: Run lint, focused benchmarks, and the full suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests scripts
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history_benchmark.py -v
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
```

Expected: Ruff passes; benchmark smoke passes; the full suite passes.

- [ ] **Step 5: Commit benchmark coverage**

```powershell
git add scripts/benchmark_topology_history.py tests/test_topology_history_benchmark.py
git commit -m "test: benchmark scoped topology queries"
```

---

### Task 6: Final Scope and Runtime Verification

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: completed Tasks 1-5.
- Produces: clean-tree, test, lint, migration, and benchmark evidence for handoff.

- [ ] **Step 1: Verify migration and query-focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py tests/test_topology_history.py tests/test_cluster_topology.py tests/test_api.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run final quality checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests scripts
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
git diff --check
git status --short
```

Expected: Ruff passes, the full suite passes, no whitespace errors are reported, and the worktree is clean.

- [ ] **Step 3: Record the final implementation range**

Run:

```powershell
git log --oneline ea45906..HEAD
git diff --stat ea45906..HEAD
git rev-parse --short HEAD
```

Expected: five implementation commits follow the approved design commit, and the final commit ID is available for handoff.
