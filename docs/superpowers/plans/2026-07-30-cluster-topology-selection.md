# 集群拓扑按目标加载实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 集群模式进入后等待用户选择目标集群，再显示该集群及其直接外部连接，并在右侧显示不含误导性空连接提示的简要概览。

**Architecture:** 保留现有 `/api/topology/clusters` 全局聚合接口，将请求触发时机从“切换模式”延后到“选择集群”。前端从全局结果中构造以目标集群为中心的一跳子图，并用独立的集群概览渲染函数管理右侧默认状态。

**Tech Stack:** Python 3.10、FastAPI、Jinja2、原生 JavaScript、Cytoscape.js、pytest、Node.js 语法检查。

## Global Constraints

- 不修改数据库模型、迁移或集群拓扑聚合规则。
- 不增加新的后端接口或第三方依赖。
- 集群模式未选择目标时不得请求 `/api/topology/clusters`。
- 右侧默认概览只显示集群名称、成员设备数和外部连接组数。
- 连接明细仍通过点击节点或连线查看。
- 当前连接与 `1d`、`3d`、`7d` 历史范围规则保持不变。
- 不使用 Git。

---

## 文件结构

- 修改 `app/templates/topology.html`：将集群筛选文案改为明确的目标选择，并提供空值占位项。
- 修改 `app/static/js/topology.js`：负责等待状态、延迟加载、一跳子图过滤、集群概览和事件路由。
- 修改 `tests/test_topology_frontend.py`：以静态契约测试保护关键交互和中文文案。
- 生成 `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`：交付包含本次改造的 Linux 部署包。

### Task 1: 明确集群选择入口与前端契约

**Files:**
- Modify: `app/templates/topology.html:24-28`
- Modify: `tests/test_topology_frontend.py`

**Interfaces:**
- Consumes: Jinja2 提供的 `clusters` 列表。
- Produces: `#cluster-filter`，空值表示“尚未选择”，非空值保持 `cluster-<id>` 格式。

- [ ] **Step 1: 添加失败的模板契约测试**

在 `tests/test_topology_frontend.py` 中加入：

```python
TOPOLOGY_TEMPLATE = Path("app/templates/topology.html")


def test_cluster_filter_requires_explicit_target_selection():
    template = TOPOLOGY_TEMPLATE.read_text(encoding="utf-8")

    assert "目标集群" in template
    assert '<option value="">选择一个集群</option>' in template
    assert "全部集群和设备" not in template
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py::test_cluster_filter_requires_explicit_target_selection -q
```

Expected: FAIL，因为模板仍包含“集群筛选”和“全部集群和设备”。

- [ ] **Step 3: 修改集群选择框**

将模板中的集群筛选改为：

```html
<label id="cluster-filter-wrap" hidden>目标集群
  <select id="cluster-filter">
    <option value="">选择一个集群</option>
    {% for cluster in clusters %}
    <option value="cluster-{{ cluster.id }}">{{ cluster.name }}</option>
    {% endfor %}
  </select>
</label>
```

- [ ] **Step 4: 运行模板契约测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py::test_cluster_filter_requires_explicit_target_selection -q
```

Expected: PASS。

### Task 2: 实现等待选择、按目标显示和右侧概览

**Files:**
- Modify: `app/static/js/topology.js`
- Modify: `tests/test_topology_frontend.py`

**Interfaces:**
- Consumes: `source.nodes`、`source.edges`、`clusterSelect.value` 和现有连接筛选控件。
- Produces:
  - `showDeviceWaiting(): void`
  - `showClusterWaiting(): void`
  - `renderClusterOverview(elements: Array<object>): void`
  - `loadClusters(): Promise<void>`，仅在已选择集群时请求数据。
  - `filteredElements(): Array<object>`，集群模式只返回目标集群一跳子图。

- [ ] **Step 1: 添加失败的集群交互契约测试**

在 `tests/test_topology_frontend.py` 中加入：

```python
def test_cluster_mode_waits_for_explicit_selection_before_loading():
    script = script_text()

    assert "const showClusterWaiting" in script
    assert 'if (!clusterSelect.value) return showClusterWaiting();' in script
    assert 'clusterSelect.addEventListener("change", load)' in script
    assert 'if (mode === "cluster") {' in script
    assert "showClusterWaiting();" in script


def test_cluster_default_drawer_uses_summary_instead_of_empty_connection_table():
    script = script_text()

    assert "const renderClusterOverview" in script
    assert "成员设备" in script
    assert "外部连接组" in script
    assert "点击拓扑中的节点或连线查看详细信息" in script
    assert 'renderDrawer("集群拓扑"' not in script


def test_cluster_subgraph_keeps_only_target_and_direct_peers():
    script = script_text()

    assert "const selectedClusterId = clusterSelect.value" in script
    assert "edge.data.source === selectedClusterId" in script
    assert "edge.data.target === selectedClusterId" in script
    assert "node.data.id === selectedClusterId || peerIds.has(node.data.id)" in script
```

- [ ] **Step 2: 运行新增测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py -q
```

Expected: 新增的三个测试 FAIL。

- [ ] **Step 3: 增加通用的图和右侧状态重置函数**

在 `topology.js` 的状态声明之后加入：

```javascript
const destroyGraph = () => {
  source = null;
  if (graph) graph.destroy();
  graph = null;
};

const renderDrawerPrompt = () => {
  drawer.innerHTML = `<p class="eyebrow">连接详情</p>
    <h2>选择连接</h2>
    <p class="muted">点击拓扑中的节点或边，查看协议、端口、状态与进程信息。</p>`;
};

const showDeviceWaiting = () => {
  destroyGraph();
  empty.hidden = false;
  empty.querySelector("h2").textContent = "等待选择目标设备";
  empty.querySelector("p").textContent =
    "拓扑将按对端 IP 聚合连接，点击节点或边查看端口与进程。";
  document.getElementById("snapshot-note").textContent =
    "选择设备后显示其最新成功快照";
  renderDrawerPrompt();
};

const showClusterWaiting = () => {
  destroyGraph();
  empty.hidden = false;
  empty.querySelector("h2").textContent = "等待选择目标集群";
  empty.querySelector("p").textContent =
    "选择集群后显示该集群与外部节点的连接关系。";
  document.getElementById("snapshot-note").textContent =
    "选择集群后显示其连接拓扑";
  renderDrawerPrompt();
};
```

所有模式切换和目标变化通过这些函数清除旧图，避免设备或上一个集群的详情残留。

- [ ] **Step 4: 收紧集群子图过滤**

将 `filteredElements()` 的集群分支明确改为：

```javascript
const selectedClusterId = clusterSelect.value;
if (mode === "cluster") {
  edges = selectedClusterId
    ? edges.filter(edge =>
      edge.data.source === selectedClusterId
      || edge.data.target === selectedClusterId
    )
    : [];
}
const peerIds = new Set(edges.flatMap(edge => [edge.data.source, edge.data.target]));
const nodes = mode === "cluster"
  ? source.nodes.filter(node =>
    node.data.id === selectedClusterId || peerIds.has(node.data.id)
  )
  : source.nodes.filter(node => node.data.kind === "server" || peerIds.has(node.data.id));
```

该逻辑在零连接时仍保留目标集群节点。

- [ ] **Step 5: 新增右侧集群概览并在绘图后调用**

在 `renderDrawer` 后加入：

```javascript
const renderClusterOverview = elements => {
  const selectedClusterId = clusterSelect.value;
  const clusterNode = source?.nodes.find(node => node.data.id === selectedClusterId);
  if (!clusterNode) return renderDrawerPrompt();
  const connectionGroups = elements.filter(item =>
    item.data.source === selectedClusterId || item.data.target === selectedClusterId
  ).length;
  const memberCount = clusterNode.data.members?.length || 0;
  drawer.innerHTML = `<p class="eyebrow">集群概览</p>
    <h2>${escapeHtml(clusterNode.data.label)}</h2>
    <div class="detail-count">${memberCount}<small>成员设备</small></div>
    <div class="detail-count">${connectionGroups}<small>外部连接组</small></div>
    <p class="muted">点击拓扑中的节点或连线查看详细信息。</p>`;
};
```

在 `draw()` 创建图并注册事件后加入：

```javascript
if (mode === "cluster") renderClusterOverview(elements);
```

筛选条件触发重绘时，概览中的连接组数随子图实时变化。

- [ ] **Step 6: 将集群加载改为选择后触发**

将 `loadClusters()` 的开始、成功和失败路径改为：

```javascript
const loadClusters = async () => {
  if (!clusterSelect.value) return showClusterWaiting();
  destroyGraph();
  empty.hidden = false;
  empty.querySelector("h2").textContent = "正在读取集群拓扑";
  empty.querySelector("p").textContent =
    windowSelect.value === "current"
      ? "系统正在读取各设备最近一次成功快照。"
      : `系统正在汇总最近 ${windowSelect.value} 内的成功快照。`;
  const response = await fetch(
    `/api/topology/clusters?window=${selectedWindow()}`
  );
  if (!response.ok) {
    const result = await response.json();
    empty.querySelector("h2").textContent = "集群拓扑加载失败";
    empty.querySelector("p").textContent = result.detail || "请稍后重试。";
    renderDrawerPrompt();
    return;
  }
  source = await response.json();
  const selectedExists = source.nodes.some(
    node => node.data.id === clusterSelect.value
  );
  if (!selectedExists) {
    destroyGraph();
    empty.querySelector("h2").textContent = "目标集群不存在";
    empty.querySelector("p").textContent = "请刷新页面后重新选择集群。";
    renderDrawerPrompt();
    return;
  }
  empty.hidden = true;
  document.getElementById("snapshot-note").textContent =
    windowSelect.value === "current"
      ? "集群模式使用各设备最近一次成功快照，数据并非严格同一时刻"
      : `最近 ${windowSelect.value} · 各设备当前基线时间可能不同`;
  draw();
  if (source.warnings?.length) toast(source.warnings[0], "error");
};
```

不得再调用 `renderDrawer("集群拓扑", ..., [])`。

- [ ] **Step 7: 修改事件路由和模式切换**

事件绑定修改为：

```javascript
clusterSelect.addEventListener("change", load);
windowSelect.addEventListener("change", load);
```

模式切换的集群分支修改为：

```javascript
if (mode === "cluster") {
  showClusterWaiting();
} else {
  if (deviceSelect.value) load();
  else showDeviceWaiting();
}
```

`load()` 保持以下守卫：

```javascript
if (mode === "cluster") return loadClusters();
if (!deviceSelect.value) return showDeviceWaiting();
```

因此未选择集群时，切换时间范围只会恢复等待状态，不会发起网络请求。

- [ ] **Step 8: 运行前端测试和语法检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py -q
node --check app/static/js/topology.js
.\.venv\Scripts\ruff.exe check tests/test_topology_frontend.py
```

Expected: 全部 PASS，Node 和 Ruff 退出码均为 0。

### Task 3: 回归、浏览器验收与 Linux 打包

**Files:**
- Verify: `app/templates/topology.html`
- Verify: `app/static/js/topology.js`
- Create: `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

**Interfaces:**
- Consumes: 运行中的 FastAPI 页面 `/topology` 和现有测试数据库中的集群。
- Produces: 通过验收的交互以及包含本次文件的带时间戳 Linux 部署包。

- [ ] **Step 1: 运行完整自动化检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check app tests
node --check app/static/js/device-list.js
node --check app/static/js/topology.js
node --check app/static/js/clusters.js
```

Expected: pytest 全绿，Ruff 通过，三个 JavaScript 文件语法检查退出码为 0。

- [ ] **Step 2: 启动或重启本地服务**

仅停止命令行明确包含当前工作区和 `uvicorn app.main:app` 的进程，然后运行：

```powershell
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

Expected: `http://127.0.0.1:8000/topology` 返回 HTTP 200。

- [ ] **Step 3: 使用应用内浏览器完成交互验收**

按以下顺序验证：

1. 打开 `/topology`，点击“集群模式”。
2. 确认画布显示“等待选择目标集群”，右侧显示“选择连接”。
3. 确认切换模式时没有请求 `/api/topology/clusters`。
4. 选择一个集群，确认此时才请求接口并绘制目标集群一跳子图。
5. 确认右侧显示集群名称、成员设备数、外部连接组数。
6. 确认右侧不出现“当前筛选条件下没有连接”。
7. 改变协议、状态或进程筛选，确认概览连接组数同步变化。
8. 清空集群选择，确认拓扑和旧详情清空并恢复等待状态。
9. 切回设备模式，确认选择设备后加载拓扑的原有流程正常。
10. 检查浏览器控制台没有新增 error。

- [ ] **Step 4: 生成并检查带时间戳部署包**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
tar -tzf .\connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz |
  Select-String -Pattern "app/templates/topology.html|app/static/js/topology.js"
```

Expected: 新归档文件名包含当前时间戳，并同时包含模板与脚本。

- [ ] **Step 5: 最终交付检查**

再次运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: 完整测试套件通过。报告浏览器验收结果、运行地址、部署包路径、测试数量以及现有依赖警告；不执行 Git 操作。
