# 历史拓扑 SQL 聚合性能优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `1d/3d/7d` 历史拓扑从加载全部 ORM 连接对象改为 SQLite 精确聚合，使约 137 万条原始连接的 `1d` 首次查询在参考环境中不超过 10 秒。

**Architecture:** 当前拓扑继续读取最新成功快照；历史拓扑通过 SQL 分组查询生成紧凑服务记录，再由 Python 合并规范化 IP 变体。集群 API 接收 `cluster_id` 并在服务端返回目标一跳子图，同时增加联合索引、30 秒缓存、主动失效和前端 15 秒超时。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、SQLite、原生 JavaScript、Cytoscape.js、pytest、Ruff、Node.js。

## Global Constraints

- 历史统计必须完全精确，不允许快照采样。
- 保持首次发现、最后发现、出现次数、当前/已断开、本地 IP、本地端口、PID 和最新代表记录语义。
- 保持 IPv4 映射地址、回环地址、同集群连接、集群内部地址段、重复地址所有权等现有规则。
- `current` 不进入历史 SQL 聚合路径。
- Python 版本保持 3.10，不新增第三方依赖。
- 不删除或修改现有历史原始记录。
- 不使用 Git。

---

## 文件结构

- Modify `app/services/topology_history.py`：当前快照查询、历史 SQL 分组、规范化后二次精确合并。
- Modify `app/services/topology.py`：接受预聚合服务并支持目标集群一跳裁剪。
- Create `app/services/topology_cache.py`：线程安全 30 秒 TTL 缓存。
- Modify `app/routes/api.py`：设备/集群历史缓存、`cluster_id` 校验及修改操作缓存失效。
- Modify `app/services/scan_queue.py`、`app/main.py`：成功采集后缓存失效及应用状态装配。
- Modify `app/models.py`、`app/migrations.py`：联合索引声明及旧数据库幂等迁移。
- Modify `app/static/js/topology.js`、`app/templates/topology.html`：发送 `cluster_id`、取消旧请求、15 秒超时和静态资源版本。
- Create `scripts/benchmark_topology_history.py`：137 万行独立临时数据库性能基准。
- Modify/Create tests：结果一致性、集群范围、迁移、缓存、API、前端和性能烟雾测试。

### Task 1: 建立精确 SQL 历史聚合服务

**Files:**
- Modify: `app/services/topology_history.py`
- Modify: `tests/test_topology_history.py`

**Interfaces:**
- Consumes: `Session`、设备 ID、当前成功快照 ID、`TopologyWindow`、可选源设备和入向地址限制。
- Produces:
  - `load_current_scans(session, device_ids, with_connections=True) -> dict[int, ScanRun]`
  - `aggregate_historical_connections(session, device_ids, current_scan_ids, window, now=None, source_device_ids=None, inbound_addresses=None) -> list[dict]`
- Returned dictionaries must match `aggregate_service_connections()` output keys.

- [ ] **Step 1: Add failing equivalence tests**

Extend `tests/test_topology_history.py` with tests that build historical/current scans containing mapped IPv4, changing local ports/PIDs, duplicate rows in one scan, two process names, listeners and loopbacks. Compare normalized projections:

```python
def service_projection(rows):
    keys = (
        "source_device_id", "protocol", "remote_ip", "remote_port",
        "process_name", "is_current", "first_seen", "last_seen",
        "observation_count", "observed_local_ips",
        "observed_local_ports", "observed_pids",
    )
    return sorted(
        [{key: row[key] for key in keys} for row in rows],
        key=lambda row: (
            row["source_device_id"], row["protocol"],
            row["remote_ip"], row["remote_port"], row["process_name"],
        ),
    )


def test_sql_history_matches_python_aggregation(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        historical = add_scan(
            session, device, started_at=NOW - timedelta(hours=8),
            local_port=50000, pid=100, remote_ip="::ffff:203.0.113.8",
        )
        current = add_scan(
            session, device, started_at=NOW - timedelta(hours=1),
            local_port=50123, pid=200, remote_ip="203.0.113.8",
        )
        current.connections.append(ConnectionRecord(
            protocol="tcp", address_family="ipv4", local_ip=device.host,
            local_port=50124, remote_ip="203.0.113.8", remote_port=443,
            state="ESTABLISHED", pid=201, process_name="client",
        ))
        session.commit()

        expected = aggregate_service_connections(
            [historical, current], {current.id}
        )
        actual = aggregate_historical_connections(
            session, [device.id], {current.id}, "1d", now=NOW
        )
        assert service_projection(actual) == service_projection(expected)
```

Add separate assertions for:

```python
assert actual[0]["observation_count"] == 2
assert actual[0]["observed_local_ports"] == [50000, 50123, 50124]
assert actual[0]["observed_pids"] == [100, 200, 201]
```

- [ ] **Step 2: Verify the new tests fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -q
```

Expected: FAIL because `aggregate_historical_connections` and `load_current_scans` do not exist.

- [ ] **Step 3: Extract the latest successful scan query**

Implement `load_current_scans()` by moving the ranked `row_number()` query from `load_topology_scans()`. Load only the selected latest scan IDs and optionally:

```python
options = [selectinload(ScanRun.device)]
if with_connections:
    options.append(selectinload(ScanRun.connections))
```

Return `{scan.device_id: scan}`. Keep `load_topology_scans()` as a compatibility wrapper for `current`; historical routes will stop calling it.

- [ ] **Step 4: Implement raw SQL-group rows without ORM connection loading**

Inside `aggregate_historical_connections()`:

1. Reject `window == "current"` with `ValueError`.
2. Build `cutoff = (now or datetime.now(timezone.utc)) - WINDOW_DELTAS[window]`.
3. Select eligible connection columns joined to successful scans where:

```python
or_(
    ScanRun.started_at >= cutoff,
    ScanRun.id.in_(current_scan_ids),
)
```

4. Apply optional cluster candidate restriction:

```python
or_(
    ScanRun.device_id.in_(source_device_ids),
    ConnectionRecord.remote_ip.in_(inbound_addresses),
)
```

5. Exclude `remote_ip IS NULL`.
6. Create an aggregate subquery grouped by source device, protocol, raw remote IP,
   remote port and `coalesce(process_name, "")`, returning `min(started_at)`,
   `max(started_at)`, and SQLite `group_concat(distinct ...)` for scan IDs,
   local IPs, local ports and non-null PIDs.
7. Create a ranked subquery with `row_number()` over the same raw group ordered by
   `started_at DESC, scan_id DESC, connection_id DESC`; select `position == 1`
   for the latest representative fields.

Do not select `ConnectionRecord` ORM entities anywhere in the historical function.

- [ ] **Step 5: Merge raw IP variants exactly in Python**

Parse each aggregate row into sets using helper functions:

```python
def _csv_int_set(value: str | None) -> set[int]:
    return {int(item) for item in value.split(",")} if value else set()


def _csv_str_set(value: str | None) -> set[str]:
    return set(value.split(",")) if value else set()
```

Normalize remote and local IP values with `normalize_ip_address`. Merge by the existing
service key `(device_id, protocol, normalized_remote, remote_port, process_name)`.
Union scan/local IP/local port/PID sets, take the minimum `first_seen`, maximum
`last_seen`, and use the representative row with the latest
`(started_at, scan_id, connection_id)`.

Finalize each dictionary with:

```python
all_scan_ids = bucket.pop("_scan_ids")
bucket["is_current"] = bool(all_scan_ids & current_ids)
bucket["observation_count"] = len(all_scan_ids)
bucket["observed_local_ips"] = sorted(local_ips)
bucket["observed_local_ports"] = sorted(local_ports)
bucket["observed_pids"] = sorted(pids)
```

Ensure loopback remote addresses are discarded after normalization.

- [ ] **Step 6: Run equivalence and history tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -q
.\.venv\Scripts\ruff.exe check app/services/topology_history.py tests/test_topology_history.py
```

Expected: all tests pass and Ruff reports no errors.

### Task 2: Integrate SQL services into device and scoped cluster topology

**Files:**
- Modify: `app/services/topology.py`
- Modify: `app/routes/api.py`
- Modify: `app/static/js/topology.js`
- Modify: `tests/test_api.py`
- Modify: `tests/test_cluster_topology.py`
- Modify: `tests/test_topology_frontend.py`

**Interfaces:**
- Consumes: Task 1 `load_current_scans()` and `aggregate_historical_connections()`.
- Produces:
  - `build_topology(scan_run, services=None, window="current")`
  - `build_cluster_topology(session, resolver, window="current", target_cluster_id=None)`
  - `/api/topology/clusters?window=<window>&cluster_id=<integer>`

- [ ] **Step 1: Add failing API and scoped topology tests**

Add tests asserting:

```python
response = client.get(f"/api/topology/clusters?window=1d&cluster_id={cluster.id}")
assert response.status_code == 200
assert {node["data"]["id"] for node in response.json()["nodes"]} == {
    f"cluster-{cluster.id}", "external-203.0.113.8"
}
assert client.get("/api/topology/clusters?cluster_id=999999").status_code == 404
```

Add a cross-cluster fixture with target-cluster outbound, peer-cluster inbound,
same-cluster and unrelated-cluster connections. Assert outbound and inbound remain,
same-cluster and unrelated edges are absent.

Add frontend contract assertions:

```python
assert 'params.set("cluster_id", clusterSelect.value.replace("cluster-", ""))' in script
```

- [ ] **Step 2: Verify scoped tests fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py tests/test_cluster_topology.py tests/test_topology_frontend.py -q
```

Expected: new `cluster_id` scope tests fail.

- [ ] **Step 3: Let builders accept pre-aggregated services**

Change `build_topology()`:

```python
def build_topology(
    scan_run: ScanRun,
    scans: Sequence[ScanRun] | None = None,
    window: TopologyWindow = "current",
    services: Sequence[dict] | None = None,
) -> dict:
    selected_services = (
        list(services)
        if services is not None
        else aggregate_service_connections(
            list(scans) if scans is not None else [scan_run],
            {scan_run.id},
        )
    )
```

Use `selected_services` for grouping while keeping listeners from the latest scan.

- [ ] **Step 4: Add target scope to cluster builder**

Extend `build_cluster_topology(..., target_cluster_id: int | None = None)`.
Load devices and clusters as today, validate the target cluster before history work,
and resolve all device addresses once.

For historical windows:

- load current scans with `with_connections=False`;
- derive target member IDs;
- derive normalized target member addresses and raw-compatible inbound variants
  (`x.x.x.x` and `::ffff:x.x.x.x` for IPv4);
- call `aggregate_historical_connections()` with all managed device IDs,
  target source IDs and inbound address variants.

For current, load latest scans with connections and use the existing Python aggregator.
After nodes/edges are built, if `target_cluster_id` is set, retain only the target node,
incident edges and their peer nodes.

- [ ] **Step 5: Route device historical mode through SQL aggregation**

In `/devices/{device_id}/topology`:

```python
current = load_current_scans(db, [device_id])
scan = current.get(device_id)
if scan is None:
    raise HTTPException(status_code=404, detail="该设备还没有成功采集快照")
services = None
if window != "current":
    services = aggregate_historical_connections(
        db, [device_id], {scan.id}, window
    )
return build_topology(scan, window=window, services=services)
```

- [ ] **Step 6: Validate and pass cluster ID in the API**

Add `cluster_id: int | None = Query(default=None, ge=1)` to
`get_cluster_topology()`. Require it for the page-generated request; retain `None`
support for API backward compatibility. If set and absent, return HTTP 404
`"集群不存在"`. Pass `target_cluster_id=cluster_id`.

- [ ] **Step 7: Send cluster ID from the browser**

In `loadClusters()` build:

```javascript
const params = new URLSearchParams({window: windowSelect.value});
params.set("cluster_id", clusterSelect.value.replace("cluster-", ""));
const response = await fetch(`/api/topology/clusters?${params}`);
```

Bump the topology script query version in `app/templates/topology.html`.

- [ ] **Step 8: Run topology integration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py tests/test_cluster_topology.py tests/test_api.py tests/test_topology_frontend.py -q
node --check app/static/js/topology.js
```

Expected: all selected tests pass.

### Task 3: Add indexes and idempotent old-database migration

**Files:**
- Modify: `app/models.py`
- Modify: `app/migrations.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Produces schema version `5` and indexes:
  - `ix_scan_device_status_started`
  - `ix_connection_history_service`

- [ ] **Step 1: Add failing migration tests**

Create an old SQLite schema without the new indexes, run `run_migrations(engine)` twice,
then assert:

```python
scan_indexes = {row["name"] for row in inspect(engine).get_indexes("scan_runs")}
connection_indexes = {
    row["name"] for row in inspect(engine).get_indexes("connection_records")
}
assert "ix_scan_device_status_started" in scan_indexes
assert "ix_connection_history_service" in connection_indexes
assert 5 in versions
```

- [ ] **Step 2: Verify migration tests fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py -q
```

Expected: missing index/version assertions fail.

- [ ] **Step 3: Declare and migrate indexes**

Update model table args:

```python
Index(
    "ix_scan_device_status_started",
    "device_id", "status", "started_at",
),
Index(
    "ix_connection_history_service",
    "scan_run_id", "remote_ip", "remote_port", "protocol", "process_name",
),
```

Set `LATEST_SCHEMA_VERSION = 5` and add guarded SQL:

```sql
CREATE INDEX IF NOT EXISTS ix_scan_device_status_started
ON scan_runs (device_id, status, started_at);

CREATE INDEX IF NOT EXISTS ix_connection_history_service
ON connection_records
(scan_run_id, remote_ip, remote_port, protocol, process_name);
```

Only execute each statement when its table exists.

- [ ] **Step 4: Run migration and model tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py -q
.\.venv\Scripts\ruff.exe check app/models.py app/migrations.py tests/test_migrations.py
```

Expected: all tests pass.

### Task 4: Add topology cache and active invalidation

**Files:**
- Create: `app/services/topology_cache.py`
- Modify: `app/main.py`
- Modify: `app/routes/api.py`
- Modify: `app/services/scan_queue.py`
- Create: `tests/test_topology_cache.py`
- Modify: `tests/test_scan_queue.py`

**Interfaces:**
- Produces:
  - `TopologyCache(ttl_seconds=30)`
  - `get(key: tuple) -> dict | None`
  - `put(key: tuple, value: dict) -> None`
  - `clear() -> None`
  - `ScanQueueService(..., on_successful_scan: Callable[[], None] | None = None)`

- [ ] **Step 1: Add failing cache tests**

Use a fake monotonic clock to verify deep-copy isolation, TTL expiry and clear:

```python
clock = FakeClock()
cache = TopologyCache(ttl_seconds=30, clock=clock)
value = {"nodes": [{"data": {"id": "cluster-1"}}]}
cache.put(("cluster", 1, "1d"), value)
cached = cache.get(("cluster", 1, "1d"))
cached["nodes"].clear()
assert cache.get(("cluster", 1, "1d")) == value
clock.advance(31)
assert cache.get(("cluster", 1, "1d")) is None
```

Add a scan-queue test asserting the callback runs only after a successful scan.

- [ ] **Step 2: Verify cache tests fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_cache.py tests/test_scan_queue.py -q
```

Expected: import/signature failures.

- [ ] **Step 3: Implement the cache**

Use `threading.Lock`, `time.monotonic`, `copy.deepcopy`, and a dictionary of
`key -> (expires_at, value)`. `get()` removes expired entries. `clear()` empties all
entries under the lock.

- [ ] **Step 4: Assemble and use cache in the app**

Create `app.state.topology_cache = TopologyCache(ttl_seconds=30)` before constructing
the scan queue. For history routes:

```python
cache_key = ("device", device_id, window)
cached = request.app.state.topology_cache.get(cache_key)
if cached is not None:
    return cached
result = ...
request.app.state.topology_cache.put(cache_key, result)
return result
```

Use `("cluster", cluster_id, window)` for cluster history. Do not cache `current`.

- [ ] **Step 5: Invalidate after successful scans and configuration writes**

Pass `on_successful_scan=app.state.topology_cache.clear` into `ScanQueueService`.
After a run returns success in `_execute_task`, invoke the callback after its database
commit.

Add `request: Request` to cluster create/update/delete routes and call
`request.app.state.topology_cache.clear()` only after successful commits.
Clear after device import/create/update/delete as well.

- [ ] **Step 6: Run cache and mutation tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_cache.py tests/test_scan_queue.py tests/test_api.py tests/test_clusters.py -q
```

Expected: all tests pass.

### Task 5: Add cancellation, 15-second timeout and accurate UI errors

**Files:**
- Modify: `app/static/js/topology.js`
- Modify: `app/templates/topology.html`
- Modify: `tests/test_topology_frontend.py`

**Interfaces:**
- Produces `fetchTopology(url) -> Promise<Response>` and one active request at a time.

- [ ] **Step 1: Add failing frontend contract tests**

Assert:

```python
assert "new AbortController()" in script
assert "activeTopologyController?.abort()" in script
assert "window.setTimeout" in script
assert "15000" in script
assert "历史拓扑计算超时，请稍后重试或缩短时间范围。" in script
```

- [ ] **Step 2: Verify frontend tests fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py -q
```

- [ ] **Step 3: Implement a single request helper**

Add:

```javascript
let activeTopologyController = null;

const fetchTopology = async url => {
  activeTopologyController?.abort();
  const controller = new AbortController();
  activeTopologyController = controller;
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 15000);
  try {
    return await fetch(url, {signal: controller.signal});
  } catch (error) {
    if (timedOut) {
      throw new Error("历史拓扑计算超时，请稍后重试或缩短时间范围。");
    }
    if (error.name === "AbortError") return null;
    throw error;
  } finally {
    window.clearTimeout(timeout);
    if (activeTopologyController === controller) {
      activeTopologyController = null;
    }
  }
};
```

Use the helper in both device and cluster loaders. A `null` response means a newer
selection cancelled the request and must not alter current UI. On thrown errors, destroy
the graph and display `error.message`.

- [ ] **Step 4: Cancel requests on waiting-state transitions**

Call `activeTopologyController?.abort()` in `showDeviceWaiting()` and
`showClusterWaiting()` before clearing the graph.

- [ ] **Step 5: Bump script cache version and verify**

Change the template query suffix to a new `20260731` performance version, then run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py -q
node --check app/static/js/topology.js
```

Expected: tests pass and Node exits 0.

### Task 6: Add 1.37-million-row benchmark and final validation

**Files:**
- Create: `scripts/benchmark_topology_history.py`
- Create: `tests/test_topology_history_benchmark.py`
- Create: `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

**Interfaces:**
- Benchmark CLI:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_topology_history.py --rows 1370000 --max-seconds 10
```

- [ ] **Step 1: Add a small benchmark smoke test**

The pytest test calls the benchmark builder with 5,000 rows in a temporary database,
asserts the output service count is positive and the historical function does not emit a
SQL query selecting full `connection_records` ORM entities.

- [ ] **Step 2: Implement the standalone benchmark**

Use `tempfile.TemporaryDirectory`, the real models/migrations, and SQLite batched
`executemany` inserts. Generate 10 devices, 288 scans per device, and distribute exactly
the requested connection row count across scans with repeated service keys.

Start timing only after inserts and `ANALYZE`. Measure `time.perf_counter()` and
`tracemalloc` around `aggregate_historical_connections()`. Print:

```text
raw_rows=1370000
service_groups=<count>
elapsed_seconds=<seconds>
peak_python_mib=<MiB>
target_seconds=10
```

Exit nonzero if elapsed exceeds `--max-seconds`. Temporary data is automatically deleted.

- [ ] **Step 3: Run functional tests before the large benchmark**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests scripts
node --check app/static/js/device-list.js
node --check app/static/js/topology.js
node --check app/static/js/clusters.js
```

Expected: all tests and checks pass.

- [ ] **Step 4: Run the 1.37-million-row cold benchmark**

Run:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_topology_history.py --rows 1370000 --max-seconds 10
```

Expected: exit 0, `elapsed_seconds <= 10`, and Python peak memory is materially below
the old ORM-loading approach.

If the query exceeds 10 seconds, use `EXPLAIN QUERY PLAN` to confirm both new indexes,
then reduce intermediate sorting by materializing eligible scan IDs before aggregation.
Do not weaken exactness or increase the accepted threshold.

- [ ] **Step 5: Restart and browser-validate**

Restart only the verified local uvicorn process. In the in-app browser verify:

- cluster mode waits for a selection;
- selecting a cluster sends `cluster_id`;
- `1d` completes and renders;
- rapid `1d/3d/7d` changes do not allow stale responses to overwrite the latest choice;
- simulated timeout shows the Chinese timeout message;
- device `1d` still shows accurate history details;
- browser console has no errors.

- [ ] **Step 6: Package and inspect**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
```

Verify the new timestamped archive contains:

```text
app/services/topology_history.py
app/services/topology_cache.py
app/services/topology.py
app/migrations.py
app/static/js/topology.js
scripts/benchmark_topology_history.py
```

- [ ] **Step 7: Final regression**

Run the full pytest suite again. Report test count, benchmark time and memory, browser
result, service URL, package path, `.env` warning, missing `wheelhouse` warning, migration
startup note, and confirm no Git operations were used.
