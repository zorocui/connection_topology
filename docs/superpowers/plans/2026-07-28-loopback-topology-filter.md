# 拓扑环回连接过滤 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设备模式和集群模式中隐藏 IPv4、IPv6 环回连接，同时完整保留数据库原始记录。

**Architecture:** 在现有共享 IP 地址工具中增加纯函数 `is_loopback_address`，复用已有归一化规则并通过 Python 标准库判断环回地址。两个拓扑构建函数在节点和边聚合前过滤环回对端；采集器、ORM 模型和数据库不做修改。

**Tech Stack:** Python 3.10、标准库 `ipaddress`、FastAPI、SQLAlchemy、Pytest、Ruff

## Global Constraints

- 设备模式和集群模式都隐藏整个 IPv4 `127.0.0.0/8` 和 IPv6 `::1`。
- `::ffff:127.0.0.1` 先归一化为 IPv4，再按环回连接隐藏。
- 数据库原始连接、快照连接数量和历史审计数据保持不变。
- 不隐藏设备连接自身业务 IP 的记录。
- 普通 IPv4、原生 IPv6、监听记录及现有地址归一化行为保持不变。
- 使用项目现有 Python 3.10 虚拟环境 `.venv`。
- 按用户要求不执行 Git、分支、提交或合并操作。

---

### Task 1: 共享环回地址判断

**Files:**
- Modify: `app/collectors/base.py:6-25`
- Test: `tests/test_ip_normalization.py`

**Interfaces:**
- Consumes: `normalize_ip_address(address: str | None) -> str | None`
- Produces: `is_loopback_address(address: str | None) -> bool`

- [ ] **Step 1: 写入失败测试**

将测试文件的导入改为：

```python
from app.collectors.base import (
    address_family,
    is_loopback_address,
    normalize_ip_address,
)
```

在 `tests/test_ip_normalization.py` 末尾加入：

```python
def test_loopback_address_detection():
    assert is_loopback_address("127.0.0.1")
    assert is_loopback_address("127.23.45.67")
    assert is_loopback_address("::1")
    assert is_loopback_address("::ffff:127.0.0.1")


def test_non_loopback_and_invalid_address_detection():
    assert not is_loopback_address("10.160.79.21")
    assert not is_loopback_address("2001:db8::1")
    assert not is_loopback_address("not-an-ip")
    assert not is_loopback_address(None)
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ip_normalization.py -q
```

Expected: 测试收集阶段因无法从 `app.collectors.base` 导入
`is_loopback_address` 而失败。

- [ ] **Step 3: 实现最小共享函数**

在 `app/collectors/base.py` 的 `normalize_ip_address` 后加入：

```python
def is_loopback_address(address: str | None) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
```

- [ ] **Step 4: 运行共享工具测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ip_normalization.py -q
```

Expected: `6 passed`。

---

### Task 2: 设备拓扑过滤环回连接

**Files:**
- Modify: `app/services/topology.py:10,45-56`
- Test: `tests/test_topology_normalization.py`

**Interfaces:**
- Consumes: `is_loopback_address(address: str | None) -> bool`
- Produces: `build_topology(scan_run: ScanRun) -> dict` 不再产生环回对端节点或边

- [ ] **Step 1: 写入失败测试**

在 `tests/test_topology_normalization.py` 顶部加入 `import pytest`，并在文件末尾加入：

```python
@pytest.mark.parametrize(
    "remote_ip",
    ["127.0.0.1", "127.23.45.67", "::1", "::ffff:127.0.0.1"],
)
def test_device_topology_hides_loopback_connections_without_mutating_scan(remote_ip):
    scan = make_scan(1, remote_ip)

    topology = build_topology(scan)

    assert topology["edges"] == []
    assert len(topology["nodes"]) == 1
    assert len(scan.connections) == 1
    assert scan.connection_count == 1


def test_device_topology_keeps_native_external_ipv6():
    scan = make_scan(1, "2001:db8::1")

    topology = build_topology(scan)

    assert len(topology["edges"]) == 1
    assert topology["nodes"][1]["data"]["subtitle"] == "2001:db8::1"
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_normalization.py -q
```

Expected: 四个环回地址用例仍产生拓扑边，因此失败。

- [ ] **Step 3: 在设备拓扑聚合前过滤**

将 `app/services/topology.py` 的共享工具导入改为：

```python
from app.collectors.base import (
    address_family,
    is_loopback_address,
    normalize_ip_address,
)
```

将 `build_topology` 的连接循环改为：

```python
    for row in scan_run.connections:
        if row.remote_ip is None:
            listeners.append(connection_dict(row))
        elif is_loopback_address(row.remote_ip):
            continue
        else:
            normalized_remote = normalize_ip_address(row.remote_ip)
            assert normalized_remote is not None
            groups[normalized_remote].append(row)
```

- [ ] **Step 4: 运行设备拓扑测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_normalization.py -q
```

Expected: `8 passed`。

---

### Task 3: 集群拓扑过滤环回连接

**Files:**
- Modify: `app/services/topology.py:247-258`
- Test: `tests/test_cluster_topology.py`

**Interfaces:**
- Consumes: `is_loopback_address(address: str | None) -> bool`
- Produces: `build_cluster_topology(session: Session, resolver: HostAddressResolver) -> dict` 不再产生环回外部节点或边

- [ ] **Step 1: 写入失败测试**

在 `tests/test_cluster_topology.py` 末尾加入：

```python
def test_cluster_topology_hides_loopbacks_and_preserves_database_records(app):
    with app.state.session_factory() as session:
        source = add_device(session, app, "source", "10.160.79.20")
        remotes = [
            "127.0.0.1",
            "127.23.45.67",
            "::1",
            "::ffff:127.0.0.1",
            "2001:db8::1",
        ]
        add_scan(session, source, remotes)
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())
        node_ids = {node["data"]["id"] for node in topology["nodes"]}
        stored_count = session.query(ConnectionRecord).count()

        assert "external-127.0.0.1" not in node_ids
        assert "external-127.23.45.67" not in node_ids
        assert "external-::1" not in node_ids
        assert "external-2001:db8::1" in node_ids
        assert stored_count == len(remotes)
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py -q
```

Expected: 集群拓扑仍包含环回外部节点，因此失败。

- [ ] **Step 3: 在集群拓扑聚合前过滤**

将 `build_cluster_topology` 的连接循环开头改为：

```python
        for connection in scan.connections:
            if connection.remote_ip is None:
                continue
            if is_loopback_address(connection.remote_ip):
                continue
            normalized_remote = normalize_ip_address(connection.remote_ip)
            assert normalized_remote is not None
```

其余设备匹配、集群内连接过滤和外部节点聚合逻辑保持不变。

- [ ] **Step 4: 运行集群拓扑测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py -q
```

Expected: `3 passed`。

---

### Task 4: 文档、完整验证与服务重启

**Files:**
- Modify: `README.md`
- Verify: `app/collectors/base.py`
- Verify: `app/services/topology.py`
- Verify: `tests/test_ip_normalization.py`
- Verify: `tests/test_topology_normalization.py`
- Verify: `tests/test_cluster_topology.py`

**Interfaces:**
- Consumes: 已完成的共享判断和两个拓扑过滤路径
- Produces: 用户可访问、已重启且通过完整测试的本地服务

- [ ] **Step 1: 更新用户文档**

在 README 的地址归一化说明后加入：

```markdown
设备模式和集群模式会隐藏标准环回连接，包括 IPv4 的整个
`127.0.0.0/8`、IPv6 的 `::1` 及其 IPv4 映射形式。原始连接仍完整保存在
数据库和历史快照中。
```

- [ ] **Step 2: 运行代码检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
```

Expected: `All checks passed!`

- [ ] **Step 3: 运行完整测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: 所有测试通过，仅允许现有 Starlette `TestClient` 弃用警告。

- [ ] **Step 4: 精确重启本项目 Uvicorn**

先查询本项目 Uvicorn 启动进程：

```powershell
$uvicornRoot = Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -eq 'python.exe' -and
    $_.CommandLine -match 'uvicorn app\.main:app' -and
    $_.CommandLine -match '连接拓扑图'
  }
$uvicornChildren = Get-CimInstance Win32_Process |
  Where-Object { $_.ParentProcessId -in $uvicornRoot.ProcessId }
$uvicornRoot + $uvicornChildren |
  Select-Object ProcessId, ParentProcessId, CommandLine
```

先停止 `$uvicornChildren` 的精确进程 ID，再停止 `$uvicornRoot` 的精确进程
ID，不停止其他 Python 进程。随后启动：

```powershell
Start-Process `
  -FilePath '.\.venv\Scripts\python.exe' `
  -ArgumentList '-m uvicorn app.main:app --host 127.0.0.1 --port 8000' `
  -WorkingDirectory 'C:\Users\czh\Desktop\连接拓扑图' `
  -WindowStyle Hidden
```

- [ ] **Step 5: 验证服务和集群拓扑接口**

Run:

```powershell
$home = Invoke-WebRequest `
  -Uri 'http://127.0.0.1:8000/' `
  -UseBasicParsing `
  -TimeoutSec 10
$topology = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8000/api/topology/clusters' `
  -TimeoutSec 15
$loopbacks = $topology.nodes |
  Where-Object {
    $_.data.kind -eq 'external' -and
    ($_.data.label -eq '::1' -or $_.data.label -like '127.*')
  }
[PSCustomObject]@{
  HomeStatus = $home.StatusCode
  LoopbackNodeCount = @($loopbacks).Count
}
```

Expected: `HomeStatus` 为 `200`，`LoopbackNodeCount` 为 `0`。
