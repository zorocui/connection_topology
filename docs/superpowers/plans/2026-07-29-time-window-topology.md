# 按时间范围查看连接拓扑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为设备拓扑和集群拓扑增加“当前、1d、3d、7d”时间范围，并将只存在于历史快照的连接显示为灰色。

**Architecture:** 新增一个专门的历史拓扑聚合服务，负责选择每台设备的当前基线、读取时间窗口内的成功扫描，并按稳定的服务身份聚合连接。现有 `topology.py` 继续负责把聚合结果映射为设备或集群拓扑，API 只负责校验 `window` 并调用服务；前端保留当前筛选和布局机制，只增加时间范围、状态样式与历史详情。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、SQLite、Jinja2、原生 JavaScript、Cytoscape.js、pytest、Ruff。

## Global Constraints

- Python 版本保持 `>=3.10,<3.11`，不新增第三方依赖。
- `window` 只允许 `current`、`1d`、`3d`、`7d`，默认值为 `current`。
- 时间窗口按服务器当前时间向前 24、72、168 小时计算。
- 每台设备最近一次成功扫描始终作为当前基线，即使其早于所选时间窗口。
- 失败、排队中和运行中的扫描不参与历史拓扑。
- 服务身份固定为“源设备、协议、归一化远端 IP、远端端口、进程名称”，不包含本地临时端口和 PID。
- 同一拓扑边只要包含任意当前服务，就使用当前连接颜色；只有纯历史边显示灰色。
- 监听端口只来自最近一次成功扫描，不纳入历史聚合。
- 保留现有 IP 归一化、环回连接隐藏、集群内部连接隐藏和紧凑布局行为。
- 所有新增用户可见文案使用中文，专业缩写 `IP`、`PID`、`1d`、`3d`、`7d` 保留。
- 按用户要求暂不使用 Git；每个任务末尾执行测试检查点，不创建提交。

---

## File Structure

- Create: `app/services/topology_history.py`
  - 定义时间范围类型、时间边界、成功扫描选择和服务级历史聚合。
- Modify: `app/services/topology.py`
  - 使用聚合服务生成设备拓扑和集群拓扑，继续负责节点映射、地址归属及内部连接过滤。
- Modify: `app/routes/api.py`
  - 为设备与集群拓扑接口增加经过 FastAPI 校验的 `window` 参数。
- Modify: `app/templates/topology.html`
  - 增加时间范围控件和已断开连接图例。
- Modify: `app/static/js/topology.js`
  - 携带时间范围请求、渲染历史状态、更新详情与筛选后的边状态。
- Modify: `app/static/css/app.css`
  - 设置时间范围控件、灰色历史连线图例和状态标签样式。
- Create: `tests/test_topology_history.py`
  - 覆盖窗口选择、当前基线和稳定服务身份聚合。
- Modify: `tests/test_topology_normalization.py`
  - 覆盖设备拓扑当前/历史/混合状态及现有地址规则回归。
- Modify: `tests/test_cluster_topology.py`
  - 覆盖集群模式多设备基线、历史边和内部连接过滤。
- Modify: `tests/test_api.py`
  - 覆盖接口默认参数、合法时间范围和非法参数。
- Modify: `tests/test_pages.py`
  - 覆盖时间范围控件和图例的页面结构。
- Modify: `tests/test_topology_frontend.py`
  - 覆盖请求参数、灰色边选择器、状态重算和中文详情。

---

### Task 1: 历史快照选择与服务级聚合

**Files:**

- Create: `app/services/topology_history.py`
- Create: `tests/test_topology_history.py`

**Interfaces:**

- Produces: `TopologyWindow = Literal["current", "1d", "3d", "7d"]`
- Produces: `load_topology_scans(session, device_ids, window, now=None) -> tuple[dict[int, ScanRun], list[ScanRun]]`
- Produces: `aggregate_service_connections(scans, current_scan_ids) -> list[dict]`
- Produces aggregate fields: `source_device_id`, `is_current`, `first_seen`, `last_seen`, `observation_count`, `observed_local_ips`, `observed_local_ports`, `observed_pids`, plus all existing `connection_dict` fields.
- Consumes: `normalize_ip_address` and `is_loopback_address` from `app.collectors.base`.

- [ ] **Step 1: Write failing tests for window selection**

Create `tests/test_topology_history.py` with helpers that persist one device and timestamped scans. The first tests must prove that only successful scans in the requested window are loaded while an old latest baseline is still retained:

```python
from datetime import datetime, timedelta, timezone

from app.models import (
    ConnectionRecord,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.topology_history import load_topology_scans


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)


def add_device(session, app, name="source", host="10.0.0.10"):
    device = Device(
        name=name,
        host=host,
        os_type=OSType.LINUX,
        port=22,
        username=name,
        encrypted_password=app.state.cipher.encrypt("secret"),
    )
    session.add(device)
    session.flush()
    return device


def add_scan(
    session,
    device,
    *,
    started_at,
    status=ScanStatus.SUCCESS,
    local_port=50000,
    pid=100,
    remote_ip="203.0.113.8",
    remote_port=443,
    process_name="client",
):
    scan = ScanRun(
        device_id=device.id,
        trigger_type=ScanTrigger.MANUAL,
        status=status,
        started_at=started_at,
        finished_at=started_at,
        connection_count=1,
    )
    session.add(scan)
    session.flush()
    scan.connections.append(
        ConnectionRecord(
            protocol="tcp",
            address_family="ipv4",
            local_ip=device.host,
            local_port=local_port,
            remote_ip=remote_ip,
            remote_port=remote_port,
            state="ESTABLISHED",
            pid=pid,
            process_name=process_name,
        )
    )
    session.flush()
    return scan


def test_load_topology_scans_uses_window_and_success_only(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        old = add_scan(session, device, started_at=NOW - timedelta(days=2))
        recent = add_scan(session, device, started_at=NOW - timedelta(hours=12))
        add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            status=ScanStatus.FAILED,
        )
        session.commit()

        current, scans = load_topology_scans(
            session, [device.id], "1d", now=NOW
        )

        assert current[device.id].id == recent.id
        assert [scan.id for scan in scans] == [recent.id]
        assert old.id not in {scan.id for scan in scans}


def test_load_topology_scans_keeps_old_current_baseline(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        old_current = add_scan(
            session, device, started_at=NOW - timedelta(days=10)
        )
        session.commit()

        current, scans = load_topology_scans(
            session, [device.id], "7d", now=NOW
        )

        assert current[device.id].id == old_current.id
        assert [scan.id for scan in scans] == [old_current.id]
```

- [ ] **Step 2: Run the window tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'app.services.topology_history'`.

- [ ] **Step 3: Implement window validation constants and bounded scan loading**

Create `app/services/topology_history.py` with a SQL window query so selecting each latest scan does not require loading all retained history:

```python
from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import is_loopback_address, normalize_ip_address
from app.models import ConnectionRecord, ScanRun, ScanStatus


TopologyWindow = Literal["current", "1d", "3d", "7d"]
WINDOW_DELTAS = {
    "1d": timedelta(hours=24),
    "3d": timedelta(hours=72),
    "7d": timedelta(hours=168),
}


def load_topology_scans(
    session: Session,
    device_ids: Collection[int],
    window: TopologyWindow,
    now: datetime | None = None,
) -> tuple[dict[int, ScanRun], list[ScanRun]]:
    ids = sorted(set(device_ids))
    if not ids:
        return {}, []

    ranked = (
        select(
            ScanRun.id.label("scan_id"),
            ScanRun.device_id.label("device_id"),
            func.row_number()
            .over(
                partition_by=ScanRun.device_id,
                order_by=(desc(ScanRun.started_at), desc(ScanRun.id)),
            )
            .label("position"),
        )
        .where(
            ScanRun.device_id.in_(ids),
            ScanRun.status == ScanStatus.SUCCESS,
        )
        .subquery()
    )
    current_ids = list(
        session.scalars(select(ranked.c.scan_id).where(ranked.c.position == 1))
    )
    if not current_ids:
        return {}, []

    conditions = [ScanRun.id.in_(current_ids)]
    if window != "current":
        reference = now or datetime.now(timezone.utc)
        conditions.append(ScanRun.started_at >= reference - WINDOW_DELTAS[window])

    scans = list(
        session.scalars(
            select(ScanRun)
            .where(
                ScanRun.device_id.in_(ids),
                ScanRun.status == ScanStatus.SUCCESS,
                or_(*conditions),
            )
            .options(
                selectinload(ScanRun.device),
                selectinload(ScanRun.connections),
            )
            .order_by(ScanRun.started_at, ScanRun.id)
        )
    )
    current_id_set = set(current_ids)
    current = {
        scan.device_id: scan for scan in scans if scan.id in current_id_set
    }
    return current, scans
```

- [ ] **Step 4: Run the window tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -v
```

Expected: `2 passed`.

- [ ] **Step 5: Write failing tests for service identity, metadata, and current status**

Append tests proving local port/PID changes merge, distinct process names do not merge, observation count is per scan, mapped IPv4 is normalized, and loopback/listener rows are excluded:

```python
from app.services.topology_history import aggregate_service_connections


def test_aggregate_service_connections_ignores_local_port_and_pid(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        historical = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=8),
            local_port=50000,
            pid=100,
            remote_ip="::ffff:203.0.113.8",
        )
        current = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            local_port=50123,
            pid=200,
            remote_ip="203.0.113.8",
        )
        session.commit()

        rows = aggregate_service_connections(
            [historical, current], {current.id}
        )

        assert len(rows) == 1
        assert rows[0]["remote_ip"] == "203.0.113.8"
        assert rows[0]["is_current"] is True
        assert rows[0]["observation_count"] == 2
        assert rows[0]["observed_local_ports"] == [50000, 50123]
        assert rows[0]["observed_pids"] == [100, 200]
        assert rows[0]["first_seen"] == historical.started_at.isoformat()
        assert rows[0]["last_seen"] == current.started_at.isoformat()


def test_aggregate_service_connections_separates_process_names(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        first = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=2),
            process_name="curl",
        )
        second = add_scan(
            session,
            device,
            started_at=NOW - timedelta(hours=1),
            process_name="wget",
        )
        session.commit()

        rows = aggregate_service_connections([first, second], {second.id})

        assert {row["process_name"] for row in rows} == {"curl", "wget"}
        assert {row["process_name"]: row["is_current"] for row in rows} == {
            "curl": False,
            "wget": True,
        }


def test_aggregate_service_connections_hides_loopback_and_listeners(app):
    with app.state.session_factory() as session:
        device = add_device(session, app)
        scan = add_scan(
            session,
            device,
            started_at=NOW,
            remote_ip="127.0.0.1",
        )
        scan.connections.append(
            ConnectionRecord(
                protocol="tcp",
                address_family="ipv4",
                local_ip="0.0.0.0",
                local_port=22,
                remote_ip=None,
                remote_port=None,
                state="LISTEN",
                pid=10,
                process_name="sshd",
            )
        )
        session.commit()

        assert aggregate_service_connections([scan], {scan.id}) == []
```

- [ ] **Step 6: Run the new aggregation tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -v
```

Expected: the three new tests fail because `aggregate_service_connections` is not defined.

- [ ] **Step 7: Implement stable service aggregation**

Add the following functions to `app/services/topology_history.py`. Import `connection_dict` inside the function to avoid a module import cycle when `topology.py` later imports this module:

```python
def _service_key(device_id: int, row: ConnectionRecord) -> tuple:
    return (
        device_id,
        row.protocol,
        normalize_ip_address(row.remote_ip),
        row.remote_port,
        row.process_name or "",
    )


def aggregate_service_connections(
    scans: Sequence[ScanRun],
    current_scan_ids: Collection[int],
) -> list[dict]:
    from app.services.topology import connection_dict

    current_ids = set(current_scan_ids)
    groups: dict[tuple, dict] = {}
    ordered_scans = sorted(scans, key=lambda scan: (scan.started_at, scan.id))

    for scan in ordered_scans:
        seen_in_scan: set[tuple] = set()
        for row in scan.connections:
            if row.remote_ip is None or is_loopback_address(row.remote_ip):
                continue
            key = _service_key(scan.device_id, row)
            normalized_remote = key[2]
            if normalized_remote is None:
                continue
            values = connection_dict(row)
            values["remote_ip"] = normalized_remote
            bucket = groups.get(key)
            if bucket is None:
                bucket = {
                    **values,
                    "source_device_id": scan.device_id,
                    "scan_id": scan.id,
                    "scan_time": scan.started_at.isoformat(),
                    "is_current": False,
                    "first_seen": scan.started_at.isoformat(),
                    "last_seen": scan.started_at.isoformat(),
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
                bucket["scan_id"] = scan.id
                bucket["scan_time"] = scan.started_at.isoformat()
                bucket["last_seen"] = scan.started_at.isoformat()

            bucket["is_current"] = bucket["is_current"] or scan.id in current_ids
            bucket["_local_ips"].add(values["local_ip"])
            bucket["_local_ports"].add(values["local_port"])
            if values["pid"] is not None:
                bucket["_pids"].add(values["pid"])
            if key not in seen_in_scan:
                bucket["_scan_ids"].add(scan.id)
                seen_in_scan.add(key)

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

- [ ] **Step 8: Run the complete history service tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py -v
```

Expected: `5 passed`.

- [ ] **Step 9: Run a local quality checkpoint**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app/services/topology_history.py tests/test_topology_history.py
```

Expected: `All checks passed!`

---

### Task 2: 设备拓扑与 API 时间参数

**Files:**

- Modify: `app/services/topology.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_topology_normalization.py`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes: `load_topology_scans(...)` and `aggregate_service_connections(...)` from Task 1.
- Produces: `build_topology(current_scan, scans=None, window="current") -> dict`.
- Produces response metadata: top-level `window`; edge data fields `is_current`, `current_count`, `historical_count`, `observation_count`.
- Existing `/api/scans/{scan_id}/topology` remains fixed to that scan and calls `build_topology(scan)`.
- `/api/devices/{device_id}/topology?window=...` loads history for one device.

- [ ] **Step 1: Write failing device topology status tests**

Extend the helper in `tests/test_topology_normalization.py` or add a local helper accepting explicit timestamps. Add tests with one historical-only peer and one peer still present in the current scan:

```python
def test_device_history_marks_disconnected_and_mixed_edges():
    historical = make_scan(10, "203.0.113.10")
    historical.started_at = datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc)
    historical.connections.append(
        ConnectionRecord(
            id=11,
            protocol="tcp",
            address_family="ipv4",
            local_ip="10.160.79.20",
            local_port=51000,
            remote_ip="203.0.113.20",
            remote_port=443,
            state="ESTABLISHED",
            pid=101,
            process_name="curl",
        )
    )
    current = make_scan(20, "203.0.113.20")
    current.started_at = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)
    current.device = historical.device
    current.device_id = historical.device.id

    topology = build_topology(
        current, scans=[historical, current], window="1d"
    )
    edges = {
        edge["data"]["connections"][0]["remote_ip"]: edge["data"]
        for edge in topology["edges"]
    }

    assert topology["window"] == "1d"
    assert edges["203.0.113.10"]["is_current"] is False
    assert edges["203.0.113.10"]["historical_count"] == 1
    assert edges["203.0.113.20"]["is_current"] is True
    assert edges["203.0.113.20"]["current_count"] == 1


def test_device_history_merges_service_reconnects():
    historical = make_scan(30, "203.0.113.30")
    historical.connections[0].local_port = 50000
    historical.connections[0].pid = 100
    current = make_scan(31, "203.0.113.30")
    current.device = historical.device
    current.connections[0].local_port = 50100
    current.connections[0].pid = 200

    topology = build_topology(
        current, scans=[historical, current], window="1d"
    )
    detail = topology["edges"][0]["data"]["connections"][0]

    assert topology["edges"][0]["data"]["count"] == 1
    assert detail["observation_count"] == 2
    assert detail["observed_local_ports"] == [50000, 50100]
    assert detail["observed_pids"] == [100, 200]
```

Update the import to include the new signature:

```python
from app.services.topology import HostAddressResolver, build_topology, diff_scans
```

- [ ] **Step 2: Run device topology tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_normalization.py -v
```

Expected: new tests fail because `build_topology` does not accept `scans` or `window`.

- [ ] **Step 3: Refactor `build_topology` to consume aggregated services**

In `app/services/topology.py`, import:

```python
from collections.abc import Sequence

from app.services.topology_history import (
    TopologyWindow,
    aggregate_service_connections,
)
```

Change the function signature and grouping logic:

```python
def build_topology(
    scan_run: ScanRun,
    scans: Sequence[ScanRun] | None = None,
    window: TopologyWindow = "current",
) -> dict:
    device = scan_run.device
    server_id = f"device-{device.id}"
    selected_scans = list(scans) if scans is not None else [scan_run]
    services = aggregate_service_connections(selected_scans, {scan_run.id})
    groups: dict[str, list[dict]] = defaultdict(list)
    listeners = [
        connection_dict(row)
        for row in scan_run.connections
        if row.remote_ip is None
    ]
    for service in services:
        groups[service["remote_ip"]].append(service)

    nodes = [
        {
            "data": {
                "id": server_id,
                "label": device.name,
                "kind": "server",
                "subtitle": device.host,
            }
        }
    ]
    edges = []
    for index, (remote_ip, rows) in enumerate(sorted(groups.items())):
        peer_id = f"peer-{index}"
        hostname = next(
            (row["remote_hostname"] for row in rows if row["remote_hostname"]),
            None,
        )
        current_count = sum(1 for row in rows if row["is_current"])
        historical_count = len(rows) - current_count
        observation_count = sum(row["observation_count"] for row in rows)
        nodes.append(
            {
                "data": {
                    "id": peer_id,
                    "label": hostname or remote_ip,
                    "kind": "peer",
                    "subtitle": remote_ip,
                    "count": len(rows),
                }
            }
        )
        edges.append(
            {
                "data": {
                    "id": f"edge-{index}",
                    "source": server_id,
                    "target": peer_id,
                    "label": str(len(rows)),
                    "count": len(rows),
                    "current_count": current_count,
                    "historical_count": historical_count,
                    "observation_count": observation_count,
                    "is_current": current_count > 0,
                    "connections": rows,
                }
            }
        )
    return {
        "window": window,
        "scan": {
            "id": scan_run.id,
            "device_id": device.id,
            "device_name": device.name,
            "started_at": scan_run.started_at.isoformat(),
            "connection_count": scan_run.connection_count,
        },
        "nodes": nodes,
        "edges": edges,
        "listeners": listeners,
    }
```

Do not modify `connection_key`, `connection_dict` or `diff_scans`; scan diff must keep its existing raw-socket identity.

- [ ] **Step 4: Run normalization and existing API workflow tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_normalization.py tests/test_api.py::test_scan_and_topology_workflow -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Write failing API parameter tests**

In `tests/test_api.py`, add a test that creates a device and at least one successful scan through the existing scan workflow, then verifies default/current/window behavior and validation:

```python
def test_device_topology_validates_time_window(client, linux_device_payload):
    device = client.post("/api/devices", json=linux_device_payload).json()
    task = client.post(f"/api/devices/{device['id']}/scan").json()
    for _ in range(50):
        task_result = client.get(f"/api/scan-tasks/{task['id']}").json()
        if task_result["status"] in {"success", "failed"}:
            break
        time.sleep(0.02)
    assert task_result["status"] == "success"

    default = client.get(f"/api/devices/{device['id']}/topology")
    one_day = client.get(
        f"/api/devices/{device['id']}/topology?window=1d"
    )
    invalid = client.get(
        f"/api/devices/{device['id']}/topology?window=30d"
    )

    assert default.status_code == 200
    assert default.json()["window"] == "current"
    assert one_day.status_code == 200
    assert one_day.json()["window"] == "1d"
    assert invalid.status_code == 422
```

- [ ] **Step 6: Run the API test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py::test_device_topology_validates_time_window -v
```

Expected: failure because the endpoint ignores or does not validate `window`.

- [ ] **Step 7: Add the validated API query and historical loading**

In `app/routes/api.py`, import `Query`, `TopologyWindow`, and `load_topology_scans`:

```python
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile

from app.services.topology_history import TopologyWindow, load_topology_scans
```

Replace the device endpoint body with:

```python
@router.get("/devices/{device_id}/topology")
def get_latest_topology(
    device_id: int,
    window: TopologyWindow = Query("current"),
    db: Session = Depends(get_db),
):
    current_scans, scans = load_topology_scans(db, [device_id], window)
    scan = current_scans.get(device_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="该设备还没有成功采集快照")
    return build_topology(scan, scans=scans, window=window)
```

Keep `/api/scans/{scan_id}/topology` unchanged so a history page link always displays that exact scan rather than a moving time window.

- [ ] **Step 8: Run device topology and API tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py tests/test_topology_normalization.py tests/test_api.py -v
```

Expected: all selected tests pass.

- [ ] **Step 9: Run a local quality checkpoint**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app/services/topology.py app/routes/api.py tests/test_topology_normalization.py tests/test_api.py
```

Expected: `All checks passed!`

---

### Task 3: 集群拓扑历史窗口

**Files:**

- Modify: `app/services/topology.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_cluster_topology.py`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes: Task 1 scan loading and aggregate service records.
- Changes: `build_cluster_topology(session, resolver, window="current", now=None) -> dict`.
- Produces cluster response fields: top-level `window`; edge `is_current`, `current_count`, `historical_count`, `observation_count`.
- Keeps each service's `source_device_id`, `source_device_name`, `scan_id` and `scan_time` for details.

- [ ] **Step 1: Make cluster test helpers support explicit time and service fields**

Update `add_scan` in `tests/test_cluster_topology.py`:

```python
def add_scan(
    session,
    device,
    remotes,
    *,
    started_at=None,
    local_port=50000,
    pid=100,
    process_name="client",
):
    scan_time = started_at or datetime.now(timezone.utc)
    scan = ScanRun(
        device_id=device.id,
        trigger_type=ScanTrigger.MANUAL,
        status=ScanStatus.SUCCESS,
        started_at=scan_time,
        finished_at=scan_time,
        connection_count=len(remotes),
    )
    session.add(scan)
    session.flush()
    for remote in remotes:
        session.add(
            ConnectionRecord(
                scan_run_id=scan.id,
                protocol="tcp",
                address_family="ipv4",
                local_ip=device.host,
                local_port=local_port,
                remote_ip=remote,
                remote_port=443,
                state="ESTABLISHED",
                pid=pid,
                process_name=process_name,
            )
        )
    return scan
```

- [ ] **Step 2: Write failing cluster history tests**

Add:

```python
def test_cluster_history_marks_only_historical_edge_disconnected(app):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    with app.state.session_factory() as session:
        source = add_device(session, app, "source", "10.0.0.1")
        add_scan(
            session,
            source,
            ["203.0.113.10", "203.0.113.20"],
            started_at=now - timedelta(hours=4),
        )
        add_scan(
            session,
            source,
            ["203.0.113.20"],
            started_at=now - timedelta(hours=1),
            local_port=50100,
            pid=200,
        )
        session.commit()

        topology = build_cluster_topology(
            session, LiteralResolver(), window="1d", now=now
        )
        edges = {
            edge["data"]["target"]: edge["data"]
            for edge in topology["edges"]
        }

        assert topology["window"] == "1d"
        assert edges["external-203.0.113.10"]["is_current"] is False
        assert edges["external-203.0.113.20"]["is_current"] is True
        assert edges["external-203.0.113.20"]["count"] == 1
        assert (
            edges["external-203.0.113.20"]["connections"][0][
                "observation_count"
            ]
            == 2
        )


def test_cluster_history_uses_latest_baseline_per_device(app):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    with app.state.session_factory() as session:
        first = add_device(session, app, "first", "10.0.0.1")
        second = add_device(session, app, "second", "10.0.0.2")
        first_current = add_scan(
            session,
            first,
            ["203.0.113.1"],
            started_at=now - timedelta(hours=1),
        )
        second_current = add_scan(
            session,
            second,
            ["203.0.113.2"],
            started_at=now - timedelta(days=10),
        )
        session.commit()

        topology = build_cluster_topology(
            session, LiteralResolver(), window="7d", now=now
        )
        scan_ids = {
            detail["source_device_id"]: detail["scan_id"]
            for edge in topology["edges"]
            for detail in edge["data"]["connections"]
        }

        assert scan_ids[first.id] == first_current.id
        assert scan_ids[second.id] == second_current.id
        assert all(edge["data"]["is_current"] for edge in topology["edges"])
```

Add `timedelta` to the datetime import.

- [ ] **Step 3: Run cluster tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py -v
```

Expected: new tests fail because `build_cluster_topology` does not accept `window` or `now`.

- [ ] **Step 4: Replace latest-only scan traversal with service aggregation**

Change the signature in `app/services/topology.py`:

```python
def build_cluster_topology(
    session: Session,
    resolver: HostAddressResolver,
    window: TopologyWindow = "current",
    now: datetime | None = None,
) -> dict:
```

After loading `devices` and `clusters`, replace the all-scan/latest-scan block with:

```python
    latest_scans, scans = load_topology_scans(
        session,
        [device.id for device in devices],
        window,
        now=now,
    )
    services = aggregate_service_connections(
        scans,
        {scan.id for scan in latest_scans.values()},
    )
    devices_by_id = {device.id: device for device in devices}
```

Import `load_topology_scans` alongside the other Task 1 imports. Preserve node construction and address ownership. Replace the `for source_device in devices` / `for connection in scan.connections` edge loop with:

```python
    edge_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for service in services:
        source_device = devices_by_id[service["source_device_id"]]
        source_id = _managed_node_id(source_device)
        normalized_remote = service["remote_ip"]
        owners = address_owners.get(normalized_remote, [])
        target_device = owners[0] if len(owners) == 1 else None
        if target_device is not None:
            if target_device.id == source_device.id:
                continue
            if (
                source_device.cluster_id is not None
                and source_device.cluster_id == target_device.cluster_id
            ):
                continue
            target_id = _managed_node_id(target_device)
            target_name = target_device.name
        else:
            target_id = f"external-{normalized_remote}"
            target_name = normalized_remote
            nodes_by_id.setdefault(
                target_id,
                {
                    "data": {
                        "id": target_id,
                        "label": normalized_remote,
                        "kind": "external",
                        "subtitle": "外部地址",
                        "members": [],
                    }
                },
            )
        if source_id == target_id:
            continue
        edge_groups[(source_id, target_id)].append(
            {
                **service,
                "source_device_name": source_device.name,
                "target_device_id": (
                    target_device.id if target_device else None
                ),
                "target_device_name": (
                    target_name if target_device else None
                ),
            }
        )
```

Build each edge with:

```python
        current_count = sum(1 for detail in details if detail["is_current"])
        historical_count = len(details) - current_count
        observation_count = sum(
            detail["observation_count"] for detail in details
        )
        edges.append(
            {
                "data": {
                    "id": f"cluster-edge-{index}",
                    "source": source_id,
                    "target": target_id,
                    "label": str(len(details)),
                    "count": len(details),
                    "current_count": current_count,
                    "historical_count": historical_count,
                    "observation_count": observation_count,
                    "is_current": current_count > 0,
                    "connections": details,
                }
            }
        )
```

Add `"window": window` to the returned top-level dictionary. All existing member `scan_id` and `scan_time` values continue to come from `latest_scans`.

- [ ] **Step 5: Run cluster tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py -v
```

Expected: all tests pass, including existing internal/loopback/mapped-IP tests.

- [ ] **Step 6: Write and run a failing cluster API validation test**

Add to `tests/test_api.py`:

```python
def test_cluster_topology_validates_time_window(client):
    assert client.get("/api/topology/clusters?window=3d").status_code == 200
    assert (
        client.get("/api/topology/clusters?window=3d").json()["window"]
        == "3d"
    )
    assert client.get("/api/topology/clusters?window=30d").status_code == 422
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py::test_cluster_topology_validates_time_window -v
```

Expected: failure because the cluster endpoint ignores `window`.

- [ ] **Step 7: Pass the validated time range through the cluster API**

Replace the route with:

```python
@router.get("/topology/clusters")
def get_cluster_topology(
    request: Request,
    window: TopologyWindow = Query("current"),
    db: Session = Depends(get_db),
):
    return build_cluster_topology(
        db,
        request.app.state.address_resolver,
        window=window,
    )
```

- [ ] **Step 8: Run all backend topology tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_history.py tests/test_topology_normalization.py tests/test_cluster_topology.py tests/test_api.py -v
```

Expected: all selected tests pass.

- [ ] **Step 9: Run a local quality checkpoint**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app/services/topology.py app/routes/api.py tests/test_cluster_topology.py tests/test_api.py
```

Expected: `All checks passed!`

---

### Task 4: 时间范围控件、历史边样式与详情

**Files:**

- Modify: `app/templates/topology.html`
- Modify: `app/static/js/topology.js`
- Modify: `app/static/css/app.css`
- Modify: `tests/test_pages.py`
- Modify: `tests/test_topology_frontend.py`

**Interfaces:**

- Consumes API `window`, edge `is_current`, and aggregate connection metadata from Tasks 2–3.
- Produces DOM control `#topology-window` with values `current`, `1d`, `3d`, `7d`.
- Produces Cytoscape data attribute `is_current` as integer `1` or `0` after client-side filtering.
- Keeps protocol/state/process filtering; edge state is recalculated from the remaining filtered connections.

- [ ] **Step 1: Write failing page structure tests**

Add to `tests/test_pages.py`:

```python
def test_topology_page_contains_time_window_and_history_legend(client):
    response = client.get("/topology")

    assert response.status_code == 200
    assert response.text.count('id="topology-window"') == 1
    assert '<option value="current">当前</option>' in response.text
    assert '<option value="1d">1d</option>' in response.text
    assert '<option value="3d">3d</option>' in response.text
    assert '<option value="7d">7d</option>' in response.text
    assert "已断开连接" in response.text
```

- [ ] **Step 2: Run the page test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py::test_topology_page_contains_time_window_and_history_legend -v
```

Expected: failure because the control and legend do not exist.

- [ ] **Step 3: Add the time control and legend markup**

In `app/templates/topology.html`, insert before the protocol filter:

```html
    <label>时间范围
      <select id="topology-window">
        <option value="current">当前</option>
        <option value="1d">1d</option>
        <option value="3d">3d</option>
        <option value="7d">7d</option>
      </select>
    </label>
```

Append to `.canvas-legend`:

```html
        <span><i class="edge disconnected"></i>已断开连接</span>
```

- [ ] **Step 4: Run the page test**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py::test_topology_page_contains_time_window_and_history_legend -v
```

Expected: `1 passed`.

- [ ] **Step 5: Write failing frontend source-contract tests**

Add to `tests/test_topology_frontend.py`:

```python
def test_topology_requests_selected_time_window():
    script = TOPOLOGY_JS.read_text(encoding="utf-8")

    assert 'getElementById("topology-window")' in script
    assert "encodeURIComponent(windowSelect.value)" in script
    assert "?window=${selectedWindow()}" in script
    assert 'windowSelect.addEventListener("change", load)' in script


def test_topology_recomputes_edge_status_after_filters():
    script = TOPOLOGY_JS.read_text(encoding="utf-8")

    assert "const currentCount = connections.filter" in script
    assert "is_current: currentCount > 0 ? 1 : 0" in script
    assert 'selector: \'edge[is_current = 0]\'' in script
    assert '"line-color": "#667176"' in script


def test_topology_details_show_history_metadata_in_chinese():
    script = TOPOLOGY_JS.read_text(encoding="utf-8")

    assert 'row.is_current ? "当前" : "已断开"' in script
    assert "首次发现" in script
    assert "最后发现" in script
    assert "出现次数" in script
    assert "本地端口" in script
```

- [ ] **Step 6: Run frontend tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py -v
```

Expected: the three new tests fail because request, status selector and historical details are absent.

- [ ] **Step 7: Add time-aware loading and filtered status calculation**

Near the existing DOM declarations in `app/static/js/topology.js`, add:

```javascript
  const windowSelect = document.getElementById("topology-window");
  const selectedWindow = () => encodeURIComponent(windowSelect.value);
```

Replace the edge mapping in `filteredElements` with:

```javascript
    let edges = source.edges.map(edge => {
      const connections = edge.data.connections.filter(connectionMatches);
      const currentCount = connections.filter(row => row.is_current).length;
      return {
        ...edge,
        data: {
          ...edge.data,
          connections,
          count: connections.length,
          label: String(connections.length),
          current_count: currentCount,
          historical_count: connections.length - currentCount,
          is_current: currentCount > 0 ? 1 : 0
        }
      };
    }).filter(edge => edge.data.count > 0);
```

Change the two moving-topology fetch calls:

```javascript
    const response = await fetch(
      `/api/devices/${deviceId}/topology?window=${selectedWindow()}`
    );
```

```javascript
    const response = await fetch(
      `/api/topology/clusters?window=${selectedWindow()}`
    );
```

Register the control:

```javascript
  windowSelect.addEventListener("change", load);
```

Do not add `window` to `/api/scans/{requestedScan}/topology`; an explicit scan link remains immutable.

- [ ] **Step 8: Add historical edge styling without breaking focus**

Insert this Cytoscape style immediately after the base `edge` rule and before focus/dim rules:

```javascript
        {selector: 'edge[is_current = 0]', style: {
          "line-color": "#667176",
          "target-arrow-color": "#667176",
          "color": "#929da1"
        }},
```

When focus is applied to a disconnected edge, the generic `edge.is-focused` rule currently changes it to green. Add a more specific rule after `edge.is-focused`:

```javascript
        {selector: 'edge[is_current = 0].is-focused', style: {
          "line-color": "#929da1",
          "target-arrow-color": "#b0b9bc"
        }},
```

This preserves focus width and opacity while keeping the disconnected meaning gray.

- [ ] **Step 9: Render aggregate status and observation details**

Replace each connection card body in `connectionTable` with fields that work for both legacy listener rows and aggregate service rows:

```javascript
        ${rows.map(row => {
          const lifecycle = row.is_current === undefined
            ? escapeHtml(row.state || "—")
            : (row.is_current ? "当前" : "已断开");
          const localPorts = row.observed_local_ports?.length
            ? row.observed_local_ports.join("、")
            : row.local_port;
          const pids = row.observed_pids?.length
            ? row.observed_pids.join("、")
            : row.pid;
          const history = row.first_seen
            ? `<dl class="connection-history">
                <div><dt>首次发现</dt><dd>${escapeHtml(formatTime(row.first_seen))}</dd></div>
                <div><dt>最后发现</dt><dd>${escapeHtml(formatTime(row.last_seen))}</dd></div>
                <div><dt>出现次数</dt><dd>${escapeHtml(row.observation_count)}</dd></div>
                <div><dt>本地端口</dt><dd>${escapeHtml(localPorts || "—")}</dd></div>
              </dl>`
            : "";
          return `<article>
            <header>
              <span class="mono-tag">${escapeHtml(row.protocol.toUpperCase())}</span>
              <b class="${row.is_current === false ? "connection-disconnected" : ""}">
                ${lifecycle}
              </b>
            </header>
            <p>${escapeHtml(row.local_ip)}:${escapeHtml(row.local_port)}
              <i>→</i>
              ${escapeHtml(row.remote_ip || "*")}:${escapeHtml(row.remote_port ?? "*")}
            </p>
            <small>${escapeHtml(row.process_name || "未知进程")}
              ${pids ? `· PID ${escapeHtml(pids)}` : ""}
            </small>
            ${history}
          </article>`;
        }).join("")}
```

Use the project's existing global time formatter if one is available in `base.html`; otherwise define next to the DOM declarations:

```javascript
  const formatTime = value => new Date(value).toLocaleString("zh-CN", {
    hour12: false
  });
```

Update loading copy to reflect the selected range:

```javascript
    empty.querySelector("h2").textContent =
      windowSelect.value === "current" ? "正在读取最新快照" : "正在读取历史连接";
```

```javascript
    empty.querySelector("p").textContent =
      windowSelect.value === "current"
        ? "系统正在读取各设备最近一次成功快照。"
        : `系统正在汇总最近 ${windowSelect.value} 内的成功快照。`;
```

- [ ] **Step 10: Add control, legend, and detail styles**

In `app/static/css/app.css`, add:

```css
.canvas-legend i.edge.disconnected {
  background: #667176;
}
.connection-disconnected {
  color: #929da1 !important;
}
.connection-history {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 7px;
  margin: 11px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
.connection-history div {
  min-width: 0;
}
.connection-history dt {
  color: var(--muted);
  font-size: 8px;
}
.connection-history dd {
  margin: 4px 0 0;
  color: var(--text);
  font: 9px/1.5 var(--mono);
  overflow-wrap: anywhere;
}
```

The existing mobile `.topology-toolbar` column layout already accommodates the additional select; no new breakpoint is required.

- [ ] **Step 11: Run page and frontend tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py tests/test_topology_frontend.py -v
node --check app/static/js/topology.js
```

Expected: all pytest cases pass and `node --check` exits with code 0.

- [ ] **Step 12: Run a local quality checkpoint**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check tests/test_pages.py tests/test_topology_frontend.py
```

Expected: `All checks passed!`

---

### Task 5: 全量回归与浏览器验收

**Files:**

- Verify only: `app/`
- Verify only: `tests/`
- Verify against: `docs/superpowers/specs/2026-07-29-time-window-topology-design.md`

**Interfaces:**

- Consumes the complete backend and frontend implementation.
- Produces a verified feature with automated test evidence and real-browser evidence.

- [ ] **Step 1: Run the full automated test suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass; the previous baseline was 70 tests, and the total must be greater than 70 after adding this feature.

- [ ] **Step 2: Run full Python lint and JavaScript syntax validation**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/js/topology.js
```

Expected: Ruff prints `All checks passed!`; Node exits with code 0.

- [ ] **Step 3: Prepare deterministic browser data**

Use the existing local database and application APIs when they already contain multiple timestamped successful scans. If not, create a temporary test database through the existing SQLAlchemy models with:

- one current service also present historically but using a different local port and PID;
- one service present only in a recent historical scan;
- one loopback connection;
- two devices in the same cluster with an internal connection;
- one cluster-to-external historical-only connection.

Start the application with its normal project launcher and the temporary database URL. Do not overwrite the user's existing database.

- [ ] **Step 4: Verify device mode in a real browser**

Open `/topology`, select the prepared device, and verify:

1. “当前” is selected by default.
2. Only current services are visible.
3. Selecting `1d` adds the historical-only peer.
4. The historical-only edge is gray.
5. The mixed/current edge retains the current color.
6. Clicking each edge shows Chinese “当前” or “已断开”, first/last discovery time, observation count, local ports and PIDs.
7. The loopback connection is absent.
8. Fit, reset, node focus and edge focus still work.

- [ ] **Step 5: Verify cluster mode in a real browser**

Switch to cluster mode and verify:

1. The selected time range is retained and sent to the cluster endpoint.
2. Same-cluster internal links remain hidden.
3. Cluster-to-external historical-only edges are gray.
4. Mixed edges stay in the current color and their details distinguish service states.
5. Cluster member snapshot times still display.

- [ ] **Step 6: Verify responsive layout and console**

Check at normal desktop width and approximately 720 px viewport width:

- time range control remains usable;
- legend does not obscure important topology content;
- detail cards do not overflow horizontally;
- no new browser console errors occur.

The known Cytoscape custom `wheelSensitivity` warning, if unchanged from the existing application, is not a new failure.

- [ ] **Step 7: Record the no-Git completion checkpoint**

Report:

- total passing test count;
- Ruff result;
- JavaScript syntax result;
- desktop and narrow-width browser result;
- any known unchanged warning;
- list of modified files.

Do not stage, commit, branch, or push files.
