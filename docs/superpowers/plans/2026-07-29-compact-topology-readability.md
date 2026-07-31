# 密集拓扑可读性改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将设备和集群拓扑改为紧凑多圈布局，放大节点及连接标签，并提供焦点高亮、适配全图和重置视图能力。

**Architecture:** 后端接口和数据结构保持不变。前端在筛选完成后为当前节点计算稳定的 `layoutLevel`，继续使用内置 Cytoscape `concentric` 布局；焦点交互只增删 Cytoscape 样式类，视图按钮只控制缩放和平移。

**Tech Stack:** Python 3.10、FastAPI/Jinja2、原生 JavaScript、Cytoscape.js、CSS、Pytest、Ruff

## Global Constraints

- 保留所有对端 IP，不新增默认聚合或隐藏行为。
- 不修改后端拓扑接口、数据库、采集器或拓扑数据含义。
- 不增加 npm 包、前端框架或 Cytoscape 扩展。
- 第一圈容量为 8，第二圈为 12，之后每圈增加 4。
- 同类节点按节点 ID 稳定排序。
- 设备模式服务器居中；集群模式受管节点在内圈、外部地址在后续圈层。
- 普通节点为 `52px × 52px`，服务器为 `82px × 56px`，集群为 `94px × 64px`。
- 节点标签为 `12px`，连接标签为 `11px`，连接宽度映射为 `2px` 到 `8px`。
- 布局 `padding` 为 `36`，`minNodeSpacing` 为 `44`。
- 全图适配缩放低于 `0.7` 时，推荐视图恢复到 `0.7` 并居中主要受管节点。
- Cytoscape 缩放范围为 `0.4` 到 `2.5`。
- 视口低于 `760px` 时画布按钮纵向排列，按钮高度至少 `34px`。
- 页面中文描述保持中文，协议、PID、IP、Cytoscape 等专业术语除外。
- 使用现有 Python 3.10 虚拟环境 `.venv`。
- 按用户要求不执行 Git、分支、提交、合并或推送。

---

### Task 1: 画布视图按钮与响应式样式

**Files:**
- Modify: `app/templates/topology.html:39-55`
- Modify: `app/static/css/app.css:121-135,236-253`
- Test: `tests/test_pages.py`

**Interfaces:**
- Produces: 唯一按钮 `#fit-topology-button` 和 `#reset-topology-button`
- Produces: 容器 `.canvas-tools`
- Consumes: 现有 `.canvas-wrap` 和 `.button` 样式

- [ ] **Step 1: 写入失败测试**

在 `tests/test_pages.py` 末尾加入：

```python
def test_topology_page_contains_canvas_view_controls(client):
    response = client.get("/topology")

    assert response.text.count('id="fit-topology-button"') == 1
    assert response.text.count('id="reset-topology-button"') == 1
    assert "适配全图" in response.text
    assert "重置视图" in response.text
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py::test_topology_page_contains_canvas_view_controls -q
```

Expected: 因页面不存在两个按钮而失败。

- [ ] **Step 3: 增加画布工具条**

在 `app/templates/topology.html` 的 `.canvas-grid` 后、`#cy` 前加入：

```html
      <div class="canvas-tools" aria-label="拓扑视图控制">
        <button id="fit-topology-button" class="button" type="button">适配全图</button>
        <button id="reset-topology-button" class="button" type="button">重置视图</button>
      </div>
```

- [ ] **Step 4: 增加工具条及窄屏样式**

在 `app/static/css/app.css` 的 `.canvas-wrap` 后加入：

```css
.canvas-tools {
  position: absolute; z-index: 4; top: 14px; right: 14px;
  display: flex; gap: 8px;
}
.canvas-tools .button {
  min-height: 34px; padding: 8px 11px;
  background: rgba(7,16,20,.9); backdrop-filter: blur(5px);
}
```

在现有 `@media (max-width: 760px)` 中加入：

```css
  .canvas-tools { flex-direction: column; }
  .canvas-tools .button { min-height: 34px; }
```

- [ ] **Step 5: 运行页面测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py -q
```

Expected: `8 passed`。

---

### Task 2: 稳定多圈层级与紧凑视觉参数

**Files:**
- Modify: `app/static/js/topology.js:1-127`
- Create: `tests/test_topology_frontend.py`

**Interfaces:**
- Produces: `assignLayoutLevels(elements: Array, currentMode: string) -> Array`
- Produces: `primaryNode() -> CytoscapeCollection`
- Produces: `fitGraph() -> void`
- Produces: `resetGraphView() -> void`
- Consumes: `filteredElements() -> Array` 和全局 `graph`、`mode`

- [ ] **Step 1: 写入失败的前端契约测试**

创建 `tests/test_topology_frontend.py`：

```python
from pathlib import Path


TOPOLOGY_JS = Path("app/static/js/topology.js")


def script_text() -> str:
    return TOPOLOGY_JS.read_text(encoding="utf-8")


def test_topology_uses_compact_multiring_layout():
    script = script_text()

    assert "assignLayoutLevels" in script
    assert "8 + ringIndex * 4" in script
    assert 'node.data("layoutLevel")' in script
    assert "minNodeSpacing: 44" in script
    assert "padding: 36" in script


def test_topology_uses_readable_visual_sizes():
    script = script_text()

    assert '"width": 52' in script
    assert '"height": 52' in script
    assert '"width": 82' in script
    assert '"height": 56' in script
    assert '"width": 94' in script
    assert '"height": 64' in script
    assert '"font-size": 12' in script
    assert '"font-size": 11' in script
    assert '"width": "mapData(count, 1, 20, 2, 8)"' in script


def test_topology_preserves_readable_initial_zoom():
    script = script_text()

    assert "maxZoom: 2.5" in script
    assert "if (graph.zoom() < 0.7)" in script
    assert "graph.zoom(0.7)" in script
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py -q
```

Expected: 三项测试均因旧布局参数和缺少多圈函数而失败。

- [ ] **Step 3: 增加多圈层级计算函数**

在 `app/static/js/topology.js` 的 `renderDrawer` 后加入：

```javascript
  const assignLayoutLevels = (elements, currentMode) => {
    const edges = elements.filter(item => item.data.source && item.data.target);
    const nodes = elements
      .filter(item => !item.data.source && !item.data.target)
      .map(item => ({...item, data: {...item.data}}));
    const byId = (left, right) => String(left.data.id).localeCompare(
      String(right.data.id), undefined, {numeric: true}
    );
    const managed = nodes
      .filter(node => currentMode === "device"
        ? node.data.kind === "server"
        : node.data.kind !== "external")
      .sort(byId);
    const peers = nodes
      .filter(node => !managed.includes(node))
      .sort(byId);
    const rings = [];
    let ringIndex = 0;
    const addRings = group => {
      let offset = 0;
      while (offset < group.length) {
        const capacity = 8 + ringIndex * 4;
        rings.push(group.slice(offset, offset + capacity));
        offset += capacity;
        ringIndex += 1;
      }
    };

    if (currentMode === "device") {
      if (managed.length) rings.push(managed);
      addRings(peers);
    } else {
      addRings(managed);
      addRings(peers);
    }

    rings.forEach((ring, index) => {
      const layoutLevel = rings.length - index;
      ring.forEach(node => { node.data.layoutLevel = layoutLevel; });
    });
    return [...nodes, ...edges];
  };
```

该实现保证设备服务器独占中心层级；集群模式受管节点全部分圈完成后才放置外部
节点；容量严格为 8、12、16、20……。

- [ ] **Step 4: 增加推荐视图函数**

在 `draw` 前加入：

```javascript
  const primaryNode = () => {
    if (!graph) return null;
    const preferred = graph.nodes('[kind = "server"], [kind = "cluster"], [kind = "device"]');
    if (!preferred.length) return null;
    return preferred.sort((left, right) =>
      String(left.id()).localeCompare(String(right.id()), undefined, {numeric: true})
    )[0];
  };

  const fitGraph = () => {
    if (!graph || !graph.nodes().length) return;
    graph.fit(undefined, 36);
  };

  const resetGraphView = () => {
    if (!graph || !graph.nodes().length) return;
    fitGraph();
    if (graph.zoom() < 0.7) {
      graph.zoom(0.7);
      const primary = primaryNode();
      if (primary) graph.center(primary);
    }
  };
```

- [ ] **Step 5: 应用多圈布局和视觉尺寸**

在 `draw` 中将：

```javascript
    const elements = filteredElements();
```

改为：

```javascript
    const elements = assignLayoutLevels(filteredElements(), mode);
```

将 Cytoscape 配置替换为以下精确参数：

```javascript
      minZoom: 0.4,
      maxZoom: 2.5,
      wheelSensitivity: 0.2,
      layout: {
        name: "concentric", animate: true, animationDuration: 650, padding: 36,
        minNodeSpacing: 44,
        concentric: node => node.data("layoutLevel") || 1,
        levelWidth: () => 1
      },
```

将节点和边的基础尺寸替换为：

```javascript
        {selector: "node", style: {
          "background-color": "#162c33", "border-color": "#5f7e83", "border-width": 2,
          "label": "data(label)", "color": "#d9e7e4", "font-family": "Microsoft YaHei",
          "font-size": 12, "text-valign": "bottom", "text-margin-y": 11,
          "width": 52, "height": 52
        }},
        {selector: 'node[kind = "server"]', style: {
          "background-color": "#a6ffcb", "border-color": "#a6ffcb", "shape": "round-rectangle",
          "width": 82, "height": 56, "color": "#a6ffcb", "font-weight": 600
        }},
        {selector: 'node[kind = "cluster"]', style: {
          "background-color": "#102c2b", "border-color": "#a6ffcb", "border-width": 3,
          "shape": "round-rectangle", "width": 94, "height": 64,
          "color": "#a6ffcb", "font-weight": 600
        }},
```

保留现有 `device` 和 `external` 配色，将边样式改为：

```javascript
        {selector: "edge", style: {
          "width": "mapData(count, 1, 20, 2, 8)", "line-color": "#4e7479",
          "target-arrow-color": "#a6ffcb", "target-arrow-shape": "triangle",
          "curve-style": "bezier", "label": "data(label)", "font-size": 11,
          "color": "#a6ffcb", "text-background-color": "#091316",
          "text-background-opacity": 1, "text-background-padding": 5
        }},
```

删除旧的 `graph.fit(undefined, 55)`，在事件绑定完成后加入：

```javascript
    graph.one("layoutstop", resetGraphView);
```

- [ ] **Step 6: 运行前端契约测试和语法检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py -q
node --check app/static/js/topology.js
```

Expected: `3 passed`，Node 语法检查退出码为 `0` 且无输出。

---

### Task 3: 焦点高亮与视图按钮行为

**Files:**
- Modify: `app/static/js/topology.js:57-234`
- Modify: `tests/test_topology_frontend.py`

**Interfaces:**
- Produces: `clearFocus() -> void`
- Produces: `focusNode(node: CytoscapeNode) -> void`
- Produces: `focusEdge(edge: CytoscapeEdge) -> void`
- Consumes: Task 1 的两个按钮和 Task 2 的 `fitGraph`、`resetGraphView`

- [ ] **Step 1: 写入失败的焦点交互契约测试**

在 `tests/test_topology_frontend.py` 末尾加入：

```python
def test_topology_supports_focus_and_view_controls():
    script = script_text()

    assert "clearFocus" in script
    assert "focusNode" in script
    assert "focusEdge" in script
    assert '"is-dimmed"' in script
    assert '"is-focused"' in script
    assert '"is-neighbor"' in script
    assert 'getElementById("fit-topology-button")' in script
    assert 'getElementById("reset-topology-button")' in script
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py::test_topology_supports_focus_and_view_controls -q
```

Expected: 因缺少焦点函数和按钮事件而失败。

- [ ] **Step 3: 增加焦点函数**

在 `resetGraphView` 前加入：

```javascript
  const clearFocus = () => {
    if (!graph) return;
    graph.elements().removeClass("is-focused is-neighbor is-dimmed");
  };

  const focusNode = node => {
    clearFocus();
    const edges = node.connectedEdges();
    const neighbors = edges.connectedNodes();
    graph.elements().addClass("is-dimmed");
    node.addClass("is-focused").removeClass("is-dimmed");
    edges.addClass("is-focused").removeClass("is-dimmed");
    neighbors.addClass("is-neighbor").removeClass("is-dimmed");
  };

  const focusEdge = edge => {
    clearFocus();
    graph.elements().addClass("is-dimmed");
    edge.addClass("is-focused").removeClass("is-dimmed");
    edge.connectedNodes().addClass("is-neighbor").removeClass("is-dimmed");
  };
```

并在 `resetGraphView` 的第一行有效逻辑后调用：

```javascript
    clearFocus();
```

- [ ] **Step 4: 增加焦点样式**

在 Cytoscape `style` 数组末尾、现有 `:selected` 样式前加入：

```javascript
        {selector: "node.is-dimmed", style: {"opacity": 0.18, "text-opacity": 0.18}},
        {selector: "edge.is-dimmed", style: {"opacity": 0.1, "text-opacity": 0.1}},
        {selector: "node.is-neighbor", style: {
          "opacity": 1, "text-opacity": 1, "border-color": "#78a1a8", "border-width": 3
        }},
        {selector: "node.is-focused", style: {
          "opacity": 1, "text-opacity": 1, "border-color": "#ffb454", "border-width": 4
        }},
        {selector: "edge.is-focused", style: {
          "opacity": 1, "text-opacity": 1, "line-color": "#a6ffcb",
          "target-arrow-color": "#ffb454", "width": 8, "z-index": 20
        }},
```

- [ ] **Step 5: 接入点击和空白恢复行为**

在边点击处理器获取 `data` 前加入：

```javascript
      focusEdge(event.target);
```

在节点点击处理器获取 `rows` 前加入：

```javascript
      focusNode(node);
```

在节点处理器之后加入：

```javascript
    graph.on("tap", event => {
      if (event.target === graph) clearFocus();
    });
```

因为 `draw()` 每次销毁并新建图实例，协议、状态、进程和集群筛选重绘后不会保留
旧实例的焦点类。

- [ ] **Step 6: 接入画布按钮**

在筛选器事件绑定前加入：

```javascript
  document.getElementById("fit-topology-button").addEventListener("click", fitGraph);
  document.getElementById("reset-topology-button").addEventListener("click", resetGraphView);
```

- [ ] **Step 7: 运行前端测试和语法检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_topology_frontend.py tests/test_pages.py -q
node --check app/static/js/topology.js
```

Expected: `12 passed`，Node 语法检查退出码为 `0`。

---

### Task 4: 文档、完整检查、浏览器验证与服务重启

**Files:**
- Modify: `README.md`
- Verify: `app/templates/topology.html`
- Verify: `app/static/css/app.css`
- Verify: `app/static/js/topology.js`
- Verify: `tests/test_pages.py`
- Verify: `tests/test_topology_frontend.py`

**Interfaces:**
- Consumes: 已完成的多圈布局、视觉尺寸、焦点交互和视图按钮
- Produces: 已通过自动化与实际浏览器验证的本地服务

- [ ] **Step 1: 更新用户文档**

在 README 的拓扑说明后加入：

```markdown
对端较多时，拓扑会使用紧凑多圈布局并保持节点可读尺寸。点击节点或连线可突出
直接相关关系，点击画布空白处恢复；“适配全图”用于查看全部节点，“重置视图”
用于返回推荐缩放和中心位置。
```

- [ ] **Step 2: 运行静态检查和完整测试**

Run:

```powershell
node --check app/static/js/topology.js
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: Node 语法检查通过，Ruff 输出 `All checks passed!`，所有 Pytest 测试
通过；仅允许现有 Starlette `TestClient` 弃用警告。

- [ ] **Step 3: 精确重启本项目 Uvicorn**

查询项目启动进程及其 Python 子进程：

```powershell
$uvicornRoots = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and
  $_.CommandLine -match 'uvicorn app\.main:app' -and
  $_.CommandLine -match '连接拓扑图'
})
$rootIds = @($uvicornRoots | ForEach-Object { $_.ProcessId })
$uvicornChildren = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and $_.ParentProcessId -in $rootIds
})
$uvicornRoots + $uvicornChildren |
  Select-Object ProcessId, ParentProcessId, CommandLine
```

先停止 `$uvicornChildren` 的精确 ID，再停止 `$uvicornRoots` 的精确 ID，不停止
其他 Python 进程。随后启动：

```powershell
Start-Process `
  -FilePath '.\.venv\Scripts\python.exe' `
  -ArgumentList '-m uvicorn app.main:app --host 127.0.0.1 --port 8000' `
  -WorkingDirectory 'C:\Users\czh\Desktop\连接拓扑图' `
  -WindowStyle Hidden
```

- [ ] **Step 4: 验证服务响应**

Run:

```powershell
$response = Invoke-WebRequest `
  -Uri 'http://127.0.0.1:8000/topology' `
  -UseBasicParsing `
  -TimeoutSec 10
[PSCustomObject]@{
  StatusCode = $response.StatusCode
  HasFitButton = $response.Content.Contains('id="fit-topology-button"')
  HasResetButton = $response.Content.Contains('id="reset-topology-button"')
}
```

Expected: `StatusCode` 为 `200`，两个按钮字段都为 `True`。

- [ ] **Step 5: 使用浏览器验证桌面视图**

在 `http://127.0.0.1:8000/topology`：

1. 选择有成功快照的设备，确认普通节点视觉尺寸明显大于旧版。
2. 切换集群模式，确认受管节点位于内圈、外部节点位于外圈。
3. 点击节点，确认直接相邻关系保持清晰，其余元素淡化。
4. 点击连线，确认该线和两端节点突出，右侧详情仍显示。
5. 点击画布空白处，确认所有元素恢复。
6. 点击“适配全图”，确认全部节点进入画布。
7. 点击“重置视图”，确认焦点清除并返回推荐缩放。
8. 修改协议、状态、进程和集群筛选，确认重新布局且无旧高亮。

- [ ] **Step 6: 使用浏览器验证窄屏**

将浏览器视口设为 `720px` 宽，确认：

- 画布工具按钮纵向排列且每个按钮高度不低于 `34px`。
- 工具按钮不遮挡底部图例。
- 拓扑仍可拖动、缩放和点击。

恢复默认视口后结束验证。

