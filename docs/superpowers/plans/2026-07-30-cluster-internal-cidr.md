# 集群内部 IPv4 地址段配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个集群配置多个内部 IPv4 CIDR，并在集群拓扑中隐藏命中这些地址段且未匹配受管设备的连接。

**Architecture:** 新增规范化的 `cluster_internal_networks` 子表保存每集群 CIDR，由集群服务统一完成解析、规范化和原子替换。集群 API 和独立 `/clusters` 页面管理规则；集群拓扑一次性预加载规则，并在受管设备匹配之后过滤未受管远端地址。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、SQLite、Jinja2、原生 JavaScript、Python `ipaddress`、pytest、Ruff。

## Global Constraints

- Python 保持 `>=3.10,<3.11`，不新增第三方依赖。
- 每个集群独立配置零到 100 个 IPv4 CIDR。
- 不支持 IPv6 CIDR。
- 使用 `ipaddress.ip_network(value, strict=False)` 规范化。
- 规范化后的重复 CIDR 整体拒绝；合法重叠 CIDR允许保存且不自动合并。
- 受管设备匹配优先于内部 CIDR。
- 内部 CIDR仅影响集群模式；设备模式保持完整连接。
- 规则实时影响 `current`、`1d`、`3d`、`7d`，不修改历史连接记录。
- Excel 导入格式不增加 CIDR列，自动创建的集群规则为空。
- 新增与设备管理同级的独立集群管理页面。
- 所有新增用户可见文案使用中文。
- 按用户要求不使用 Git；每项任务以测试和静态检查作为检查点。

---

## File Structure

- Modify: `app/models.py`
  - 增加 `ClusterInternalNetwork` 模型和集群关系。
- Modify: `app/migrations.py`
  - 增加幂等 schema version 4 迁移。
- Modify: `app/services/clusters.py`
  - 增加 CIDR规范化和原子替换服务。
- Modify: `app/schemas.py`
  - 扩展集群请求和响应字段。
- Modify: `app/routes/api.py`
  - 扩展集群 CRUD，统一构建含地址段的响应。
- Modify: `app/routes/pages.py`
  - 新增 `/clusters` 页面。
- Create: `app/templates/clusters.html`
  - 独立集群管理界面。
- Create: `app/static/js/clusters.js`
  - 集群列表、创建、编辑、删除和 CIDR输入拆分。
- Modify: `app/templates/base.html`
  - 增加同级导航项并调整编号。
- Modify: `app/templates/devices.html`
  - 删除嵌入式集群管理区及其编辑/删除脚本。
- Modify: `app/static/css/app.css`
  - 增加集群管理页面布局和 CIDR标签样式。
- Modify: `app/services/topology.py`
  - 预加载并应用源集群内部 IPv4 网络规则。
- Modify: `tests/test_migrations.py`
  - 验证新表、索引和迁移版本。
- Modify: `tests/test_clusters.py`
  - 验证 CIDR服务和 CRUD。
- Modify: `tests/test_pages.py`
  - 验证页面路由、导航和职责拆分。
- Create: `tests/test_clusters_frontend.py`
  - 验证集群管理脚本契约。
- Modify: `tests/test_cluster_topology.py`
  - 验证内部 CIDR过滤优先级和历史窗口。

---

### Task 1: 地址段模型与幂等迁移

**Files:**

- Modify: `app/models.py`
- Modify: `app/migrations.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**

- Produces: `ClusterInternalNetwork(id, cluster_id, cidr)`.
- Produces: `Cluster.internal_networks: list[ClusterInternalNetwork]`.
- Produces: schema version `4`.
- Consumes: SQLite foreign keys already enabled by `app.database.create_database_engine`.

- [ ] **Step 1: Write failing migration tests**

Extend `tests/test_migrations.py`:

```python
def test_cluster_internal_network_table_and_indexes_are_created(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'networks.db'}")

    init_database(engine)
    init_database(engine)

    inspector = inspect(engine)
    assert "cluster_internal_networks" in inspector.get_table_names()
    columns = {
        column["name"]
        for column in inspector.get_columns("cluster_internal_networks")
    }
    assert columns == {"id", "cluster_id", "cidr"}
    indexes = {
        index["name"]
        for index in inspector.get_indexes("cluster_internal_networks")
    }
    assert "ix_cluster_internal_networks_cluster_id" in indexes
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT MAX(version) FROM schema_versions")
        ).scalar() == 4
```

Add to the existing legacy-upgrade test:

```python
    assert "cluster_internal_networks" in inspector.get_table_names()
```

- [ ] **Step 2: Run the migration tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py -v
```

Expected: failure because the table does not exist and schema version is 3.

- [ ] **Step 3: Add the SQLAlchemy model and relationships**

In `app/models.py`, extend `Cluster`:

```python
    internal_networks: Mapped[list[ClusterInternalNetwork]] = relationship(
        back_populates="cluster",
        cascade="all, delete-orphan",
        order_by="ClusterInternalNetwork.cidr",
    )
```

Add immediately after `Cluster`:

```python
class ClusterInternalNetwork(Base):
    __tablename__ = "cluster_internal_networks"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id",
            "cidr",
            name="uq_cluster_internal_network_cluster_cidr",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cidr: Mapped[str] = mapped_column(String(18), nullable=False)

    cluster: Mapped[Cluster] = relationship(back_populates="internal_networks")
```

The module already imports `UniqueConstraint`, `ForeignKey`, `String`, `Mapped`, `mapped_column`, and `relationship`.

- [ ] **Step 4: Add schema version 4 migration**

Change `LATEST_SCHEMA_VERSION` to `4`. Before the final schema-version insert in `app/migrations.py`, add:

```python
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS cluster_internal_networks ("
                "id INTEGER PRIMARY KEY, "
                "cluster_id INTEGER NOT NULL "
                "REFERENCES clusters(id) ON DELETE CASCADE, "
                "cidr VARCHAR(18) NOT NULL, "
                "CONSTRAINT uq_cluster_internal_network_cluster_cidr "
                "UNIQUE (cluster_id, cidr)"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_cluster_internal_networks_cluster_id "
                "ON cluster_internal_networks (cluster_id)"
            )
        )
```

- [ ] **Step 5: Run migration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py -v
```

Expected: all migration tests pass.

- [ ] **Step 6: Verify model-level uniqueness and cascade**

Add to `tests/test_clusters.py`:

```python
from sqlalchemy.exc import IntegrityError

from app.models import Cluster, ClusterInternalNetwork, Device


def test_cluster_network_unique_constraint_and_cascade(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="网络测试集群")
        cluster.internal_networks = [
            ClusterInternalNetwork(cidr="10.0.0.0/16")
        ]
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id

        session.add(
            ClusterInternalNetwork(
                cluster_id=cluster_id,
                cidr="10.0.0.0/16",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.delete(session.get(Cluster, cluster_id))
        session.commit()
        assert session.scalar(
            select(ClusterInternalNetwork).where(
                ClusterInternalNetwork.cluster_id == cluster_id
            )
        ) is None
```

Add `import pytest`.

- [ ] **Step 7: Run the model test and quality checkpoint**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_migrations.py tests/test_clusters.py::test_cluster_network_unique_constraint_and_cascade -v
.\.venv\Scripts\python.exe -m ruff check app/models.py app/migrations.py tests/test_migrations.py tests/test_clusters.py
```

Expected: tests pass and Ruff prints `All checks passed!`.

---

### Task 2: CIDR服务与集群 API

**Files:**

- Modify: `app/services/clusters.py`
- Modify: `app/schemas.py`
- Modify: `app/routes/api.py`
- Modify: `tests/test_clusters.py`
- Verify: `tests/test_imports.py`

**Interfaces:**

- Produces: `normalize_internal_networks(values: list[str]) -> list[str]`.
- Produces: `replace_internal_networks(session, cluster, cidrs) -> None`.
- Changes: `create_cluster(session, name, description=None, internal_networks=None)`.
- Produces API field: `internal_networks: list[str]`.

- [ ] **Step 1: Write failing CIDR normalization tests**

Add imports and tests to `tests/test_clusters.py`:

```python
from app.services.clusters import normalize_internal_networks


def test_normalize_internal_networks_canonicalizes_and_sorts():
    assert normalize_internal_networks(
        [" 10.96.1.8/12 ", "", "10.0.1.5/16"]
    ) == ["10.0.0.0/16", "10.96.0.0/12"]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (["10.0.0.999/16"], "内部地址段不是合法的 CIDR"),
        (["fd00:10::/64"], "内部地址段仅支持 IPv4"),
        (["10.0.1.5/16", "10.0.0.0/16"], "内部地址段重复"),
        ([f"10.0.{index}.0/24" for index in range(101)], "最多配置 100 个"),
    ],
)
def test_normalize_internal_networks_rejects_invalid_values(values, message):
    with pytest.raises(ValueError, match=message):
        normalize_internal_networks(values)


def test_normalize_internal_networks_allows_overlaps():
    assert normalize_internal_networks(
        ["10.0.0.0/16", "10.0.1.0/24"]
    ) == ["10.0.0.0/16", "10.0.1.0/24"]
```

- [ ] **Step 2: Run normalization tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_clusters.py -k internal_networks -v
```

Expected: collection fails because `normalize_internal_networks` is missing.

- [ ] **Step 3: Implement CIDR normalization and replacement**

In `app/services/clusters.py`, import:

```python
import ipaddress

from app.models import Cluster, ClusterInternalNetwork, Device
```

Add:

```python
MAX_INTERNAL_NETWORKS = 100


def normalize_internal_networks(values: list[str]) -> list[str]:
    cleaned = [value.strip() for value in values if value.strip()]
    if len(cleaned) > MAX_INTERNAL_NETWORKS:
        raise ValueError("单个集群最多配置 100 个内部地址段")
    normalized: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for value in cleaned:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError(f"内部地址段不是合法的 CIDR：{value}") from exc
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError(f"内部地址段仅支持 IPv4：{value}")
        cidr = network.with_prefixlen
        if cidr in seen:
            raise ValueError(f"内部地址段重复：{cidr}")
        seen.add(cidr)
        normalized.append((int(network.network_address), network.prefixlen, cidr))
    return [cidr for _, _, cidr in sorted(normalized)]


def replace_internal_networks(
    session: Session,
    cluster: Cluster,
    cidrs: list[str],
) -> None:
    normalized = normalize_internal_networks(cidrs)
    cluster.internal_networks.clear()
    cluster.internal_networks.extend(
        ClusterInternalNetwork(cidr=cidr) for cidr in normalized
    )
    session.flush()
```

Change `create_cluster`:

```python
def create_cluster(
    session: Session,
    name: str,
    description: str | None = None,
    internal_networks: list[str] | None = None,
) -> Cluster:
    normalized = normalize_cluster_name(name)
    if find_cluster_by_name(session, normalized):
        raise ClusterConflict("同名集群已存在")
    cluster = Cluster(
        name=normalized,
        description=description.strip() if description and description.strip() else None,
    )
    session.add(cluster)
    session.flush()
    replace_internal_networks(session, cluster, internal_networks or [])
    return cluster
```

- [ ] **Step 4: Run normalization tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_clusters.py -k internal_networks -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Extend schemas and write failing API tests**

In `app/schemas.py`, change:

```python
class ClusterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    internal_networks: list[str] = Field(default_factory=list)


class ClusterUpdate(ClusterCreate):
    pass


class ClusterRead(BaseModel):
    id: int
    name: str
    description: str | None
    internal_networks: list[str] = Field(default_factory=list)
    device_count: int = 0
    created_at: datetime
    updated_at: datetime
```

Extend the CRUD test in `tests/test_clusters.py`:

```python
    created = client.post(
        "/api/clusters",
        json={
            "name": "生产集群",
            "description": "核心业务",
            "internal_networks": ["10.0.1.5/16", "10.96.0.0/12"],
        },
    )
    assert created.status_code == 201
    cluster = created.json()
    assert cluster["internal_networks"] == [
        "10.0.0.0/16",
        "10.96.0.0/12",
    ]
    listed = client.get("/api/clusters").json()
    assert listed[0]["internal_networks"] == cluster["internal_networks"]

    updated = client.put(
        f"/api/clusters/{cluster['id']}",
        json={
            "name": "生产集群",
            "description": "已更新",
            "internal_networks": ["172.16.0.0/12"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["internal_networks"] == ["172.16.0.0/12"]
```

Add:

```python
def test_cluster_update_rolls_back_when_network_is_invalid(client):
    created = client.post(
        "/api/clusters",
        json={
            "name": "原名称",
            "internal_networks": ["10.0.0.0/16"],
        },
    ).json()

    response = client.put(
        f"/api/clusters/{created['id']}",
        json={
            "name": "不应保存",
            "internal_networks": ["fd00::/64"],
        },
    )

    assert response.status_code == 422
    current = client.get("/api/clusters").json()[0]
    assert current["name"] == "原名称"
    assert current["internal_networks"] == ["10.0.0.0/16"]
```

- [ ] **Step 6: Run API tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_clusters.py -v
```

Expected: API assertions fail because responses do not include or save `internal_networks`.

- [ ] **Step 7: Implement atomic cluster API responses**

In `app/routes/api.py`, import `selectinload`, `replace_internal_networks`, and create:

```python
def _cluster_read(cluster: Cluster, device_count: int) -> ClusterRead:
    return ClusterRead(
        id=cluster.id,
        name=cluster.name,
        description=cluster.description,
        internal_networks=[
            network.cidr for network in cluster.internal_networks
        ],
        device_count=device_count,
        created_at=cluster.created_at,
        updated_at=cluster.updated_at,
    )
```

Change the list query to preload networks:

```python
        select(Cluster, func.count(Device.id))
        .outerjoin(Device, Device.cluster_id == Cluster.id)
        .options(selectinload(Cluster.internal_networks))
        .group_by(Cluster.id)
        .order_by(Cluster.name)
```

Return `_cluster_read(cluster, count)` for each row.

In create:

```python
        cluster = create_cluster(
            db,
            payload.name,
            payload.description,
            payload.internal_networks,
        )
        db.commit()
        db.refresh(cluster)
```

Catch both `ClusterConflict` and `ValueError`, mapping conflict to 409 and CIDR errors to 422. Return `_cluster_read(cluster, 0)`.

In update, perform name conflict check, property assignment, and:

```python
        replace_internal_networks(
            db,
            cluster,
            payload.internal_networks,
        )
        db.commit()
```

Wrap the complete update in `try/except`; rollback on `ValueError` or `ClusterConflict`. Refresh the cluster and return `_cluster_read(cluster, count)`.

- [ ] **Step 8: Run cluster and import regression tests**

In `tests/test_imports.py`, extend the existing automatic-cluster assertion:

```python
        imported_cluster = session.scalar(
            select(Cluster).where(Cluster.name == "生产集群")
        )
        assert imported_cluster is not None
        assert imported_cluster.internal_networks == []
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_clusters.py tests/test_imports.py -v
.\.venv\Scripts\python.exe -m ruff check app/services/clusters.py app/schemas.py app/routes/api.py tests/test_clusters.py
```

Expected: all tests pass and Ruff passes.

---

### Task 3: 独立集群管理页面

**Files:**

- Modify: `app/routes/pages.py`
- Modify: `app/templates/base.html`
- Modify: `app/templates/devices.html`
- Create: `app/templates/clusters.html`
- Create: `app/static/js/clusters.js`
- Modify: `app/static/css/app.css`
- Modify: `tests/test_pages.py`
- Create: `tests/test_clusters_frontend.py`

**Interfaces:**

- Produces: `GET /clusters`.
- Consumes: `GET/POST/PUT/DELETE /api/clusters`.
- Produces DOM IDs: `cluster-form`, `cluster-id`, `cluster-name`, `cluster-description`, `cluster-networks`, `cluster-list`, `cancel-cluster-edit`.

- [ ] **Step 1: Write failing page responsibility tests**

Update route parameterization in `tests/test_pages.py`:

```python
@pytest.mark.parametrize(
    "path",
    ["/", "/topology", "/devices", "/clusters", "/history", "/settings"],
)
```

Add:

```python
def test_cluster_management_has_own_page_and_navigation(client):
    clusters = client.get("/clusters")
    devices = client.get("/devices")

    assert clusters.status_code == 200
    assert 'id="cluster-form"' in clusters.text
    assert 'id="cluster-networks"' in clusters.text
    assert "内部 IPv4 地址段" in clusters.text
    assert 'href="/clusters"' in clusters.text
    assert "已有集群" not in devices.text
    assert 'id="device-form"' in devices.text
    assert "快速新建集群" in devices.text
    assert "扫描所选集群" in devices.text
```

- [ ] **Step 2: Run page tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py::test_cluster_management_has_own_page_and_navigation -v
```

Expected: `/clusters` returns 404.

- [ ] **Step 3: Add page route and navigation**

In `app/routes/pages.py`:

```python
@router.get("/clusters", response_class=HTMLResponse)
def clusters_page(request: Request):
    context = _base_context(request, "clusters")
    return templates.TemplateResponse(request, "clusters.html", context)
```

In `app/templates/base.html`, insert after device management:

```html
        <a class="{{ 'active' if active == 'clusters' else '' }}" href="/clusters">
          <span>04</span>集群管理
        </a>
```

Renumber history to `05` and settings to `06`.

- [ ] **Step 4: Create the cluster page**

Create `app/templates/clusters.html`:

```html
{% extends "base.html" %}
{% block title %}集群管理 · 连接图谱{% endblock %}
{% block eyebrow %}集群与内部网络{% endblock %}
{% block heading %}集群管理{% endblock %}
{% block content %}
<div class="cluster-management-grid reveal">
  <section class="panel">
    <div class="panel-head">
      <div>
        <p class="eyebrow">集群配置</p>
        <h2 id="cluster-form-title">新建集群</h2>
      </div>
    </div>
    <form id="cluster-form" class="form-grid">
      <input id="cluster-id" type="hidden">
      <label>集群名称
        <input id="cluster-name" maxlength="100" required>
      </label>
      <label>描述
        <input id="cluster-description" maxlength="500">
      </label>
      <label class="wide">内部 IPv4 地址段
        <textarea id="cluster-networks" rows="7"
          placeholder="10.0.0.0/16&#10;10.96.0.0/12"></textarea>
        <small>支持换行、中英文逗号分隔；仅支持 IPv4 CIDR。</small>
      </label>
      <div class="form-actions wide">
        <button id="cancel-cluster-edit" class="button" type="button" hidden>取消编辑</button>
        <button class="button primary" type="submit">保存集群</button>
      </div>
    </form>
  </section>
  <section class="panel">
    <div class="panel-head">
      <div><p class="eyebrow">集群清单</p><h2>已有集群</h2></div>
      <span id="cluster-count" class="count-badge">0</span>
    </div>
    <div id="cluster-list" class="cluster-list">
      <p class="muted">正在读取集群…</p>
    </div>
  </section>
</div>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', path='js/clusters.js') }}"></script>
{% endblock %}
```

- [ ] **Step 5: Remove embedded cluster management**

Delete the complete `<section class="panel cluster-panel reveal">...</section>` from `app/templates/devices.html`.

Delete both old event-handler blocks:

```javascript
document.querySelectorAll("[data-delete-cluster]")...
document.querySelectorAll("[data-edit-cluster]")...
```

Do not remove cluster selection, quick creation, Excel import, or cluster scan controls.

- [ ] **Step 6: Write failing frontend script tests**

Create `tests/test_clusters_frontend.py`:

```python
from pathlib import Path


CLUSTERS_JS = Path("app/static/js/clusters.js")


def script_text() -> str:
    return CLUSTERS_JS.read_text(encoding="utf-8")


def test_cluster_page_splits_network_input_and_calls_crud_api():
    script = script_text()

    assert '/[\\n,，]+/' in script
    assert 'fetch("/api/clusters")' in script
    assert "method: clusterId ? \"PUT\" : \"POST\"" in script
    assert "internal_networks: parseNetworks()" in script
    assert 'method: "DELETE"' in script


def test_cluster_page_renders_network_tags_and_safe_text():
    script = script_text()

    assert "network-tag" in script
    assert "escapeHtml(cluster.name)" in script
    assert "escapeHtml(network)" in script
    assert "删除集群后，所属设备将变为未分组" in script
```

- [ ] **Step 7: Run frontend tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_clusters_frontend.py -v
```

Expected: collection fails because `clusters.js` does not exist.

- [ ] **Step 8: Implement cluster page JavaScript**

Create `app/static/js/clusters.js`:

```javascript
(() => {
  const form = document.getElementById("cluster-form");
  const idInput = document.getElementById("cluster-id");
  const nameInput = document.getElementById("cluster-name");
  const descriptionInput = document.getElementById("cluster-description");
  const networksInput = document.getElementById("cluster-networks");
  const list = document.getElementById("cluster-list");
  const count = document.getElementById("cluster-count");
  const cancel = document.getElementById("cancel-cluster-edit");
  let clusters = [];

  const parseNetworks = () => networksInput.value
    .split(/[\n,，]+/)
    .map(value => value.trim())
    .filter(Boolean);

  const resetForm = () => {
    form.reset();
    idInput.value = "";
    cancel.hidden = true;
    document.getElementById("cluster-form-title").textContent = "新建集群";
  };

  const render = () => {
    count.textContent = String(clusters.length);
    list.innerHTML = clusters.length ? clusters.map(cluster => `
      <article class="cluster-card">
        <header>
          <div>
            <b>${escapeHtml(cluster.name)}</b>
            <small>${escapeHtml(cluster.description || "暂无描述")}</small>
          </div>
          <span>${cluster.device_count} 台设备</span>
        </header>
        <div class="network-tags">
          ${cluster.internal_networks.length
            ? cluster.internal_networks.map(network =>
                `<span class="network-tag">${escapeHtml(network)}</span>`
              ).join("")
            : '<span class="muted">未配置内部地址段</span>'}
        </div>
        <footer>
          <button class="button" type="button" data-edit-cluster="${cluster.id}">编辑</button>
          <button class="button danger" type="button" data-delete-cluster="${cluster.id}">删除</button>
        </footer>
      </article>
    `).join("") : '<p class="muted">尚未创建集群。</p>';
  };

  const load = async () => {
    const response = await fetch("/api/clusters");
    if (!response.ok) return toast("读取集群失败", "error");
    clusters = await response.json();
    render();
  };

  list.addEventListener("click", async event => {
    const edit = event.target.closest("[data-edit-cluster]");
    if (edit) {
      const cluster = clusters.find(item => item.id === Number(edit.dataset.editCluster));
      if (!cluster) return;
      idInput.value = String(cluster.id);
      nameInput.value = cluster.name;
      descriptionInput.value = cluster.description || "";
      networksInput.value = cluster.internal_networks.join("\n");
      cancel.hidden = false;
      document.getElementById("cluster-form-title").textContent = "编辑集群";
      nameInput.focus();
      return;
    }
    const remove = event.target.closest("[data-delete-cluster]");
    if (!remove) return;
    if (!confirm("删除集群后，所属设备将变为未分组，内部地址段也会删除。确定继续吗？")) return;
    const response = await fetch(`/api/clusters/${remove.dataset.deleteCluster}`, {
      method: "DELETE"
    });
    if (!response.ok) return toast("删除集群失败", "error");
    toast("集群已删除");
    resetForm();
    await load();
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const clusterId = idInput.value;
    const response = await fetch(
      clusterId ? `/api/clusters/${clusterId}` : "/api/clusters",
      {
        method: clusterId ? "PUT" : "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: nameInput.value,
          description: descriptionInput.value || null,
          internal_networks: parseNetworks()
        })
      }
    );
    const result = await response.json();
    if (!response.ok) return toast(result.detail || "保存集群失败", "error");
    toast(clusterId ? "集群已更新" : "集群已创建");
    resetForm();
    await load();
  });

  cancel.addEventListener("click", resetForm);
  load();
})();
```

- [ ] **Step 9: Add focused styles**

Append to `app/static/css/app.css`:

```css
.cluster-management-grid {
  display: grid;
  grid-template-columns: minmax(320px, .7fr) minmax(480px, 1.3fr);
  gap: 20px;
  align-items: start;
}
textarea {
  width: 100%;
  border: 1px solid var(--line-bright);
  border-radius: 0;
  background: #091316;
  color: var(--text);
  padding: 10px;
  outline: none;
  resize: vertical;
  font: 10px/1.7 var(--mono);
}
textarea:focus { outline: 2px solid var(--signal); outline-offset: 2px; }
.cluster-card { padding: 18px 22px; border-bottom: 1px solid var(--line); }
.cluster-card header, .cluster-card footer {
  display: flex; justify-content: space-between; gap: 12px; align-items: center;
}
.cluster-card header b, .cluster-card header small { display: block; }
.cluster-card header small { margin-top: 6px; color: var(--muted); }
.cluster-card header > span { color: var(--muted); font: 10px var(--mono); }
.cluster-card footer { justify-content: flex-end; margin-top: 14px; }
.network-tags { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
.network-tag {
  padding: 5px 8px; border: 1px solid var(--line-bright);
  color: var(--signal); font: 9px var(--mono);
}
@media (max-width: 1100px) {
  .cluster-management-grid { grid-template-columns: 1fr; }
}
```

- [ ] **Step 10: Run page/frontend checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py tests/test_clusters_frontend.py -v
node --check app/static/js/clusters.js
.\.venv\Scripts\python.exe -m ruff check app/routes/pages.py tests/test_pages.py tests/test_clusters_frontend.py
```

Expected: all checks pass.

---

### Task 4: 集群拓扑内部 CIDR过滤

**Files:**

- Modify: `app/services/topology.py`
- Modify: `tests/test_cluster_topology.py`

**Interfaces:**

- Consumes: `Cluster.internal_networks` and canonical IPv4 CIDR strings.
- Produces: `_cluster_network_map(clusters, warnings) -> dict[int, tuple[IPv4Network, ...]]`.
- Keeps: managed address owner priority and existing warning behavior.

- [ ] **Step 1: Add a test helper for internal networks**

Import `ClusterInternalNetwork` in `tests/test_cluster_topology.py`, then add:

```python
def set_internal_networks(cluster, *cidrs):
    cluster.internal_networks = [
        ClusterInternalNetwork(cidr=cidr) for cidr in cidrs
    ]
```

- [ ] **Step 2: Write failing basic filter tests**

Add:

```python
def test_cluster_topology_hides_unmanaged_internal_cidr_and_keeps_records(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="k8s")
        set_internal_networks(cluster, "10.244.0.0/16")
        session.add(cluster)
        session.flush()
        source = add_device(session, app, "node", "192.168.1.10", cluster)
        add_scan(session, source, ["10.244.2.8", "203.0.113.8"])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())
        node_ids = {node["data"]["id"] for node in topology["nodes"]}

        assert "external-10.244.2.8" not in node_ids
        assert "external-203.0.113.8" in node_ids
        assert session.query(ConnectionRecord).count() == 2


def test_unclustered_device_does_not_apply_cluster_cidr(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="k8s")
        set_internal_networks(cluster, "10.244.0.0/16")
        session.add(cluster)
        source = add_device(session, app, "standalone", "192.168.1.20")
        add_scan(session, source, ["10.244.2.8"])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())

        assert "external-10.244.2.8" in {
            node["data"]["id"] for node in topology["nodes"]
        }
```

- [ ] **Step 3: Run basic filter tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py -k "internal_cidr or unclustered" -v
```

Expected: internal address still appears as an external node.

- [ ] **Step 4: Implement preloading and CIDR membership**

In `app/services/topology.py`, add:

```python
def _cluster_network_map(
    clusters: list[Cluster],
    warnings: list[str],
) -> dict[int, tuple[ipaddress.IPv4Network, ...]]:
    result: dict[int, tuple[ipaddress.IPv4Network, ...]] = {}
    for cluster in clusters:
        networks = []
        for rule in cluster.internal_networks:
            try:
                network = ipaddress.ip_network(rule.cidr, strict=False)
            except ValueError:
                warnings.append(
                    f"集群 {cluster.name} 存在无效内部地址段，已忽略：{rule.cidr}"
                )
                continue
            if isinstance(network, ipaddress.IPv4Network):
                networks.append(network)
        result[cluster.id] = tuple(networks)
    return result


def _is_cluster_internal_address(
    remote_ip: str,
    cluster_id: int | None,
    networks_by_cluster: dict[int, tuple[ipaddress.IPv4Network, ...]],
) -> bool:
    if cluster_id is None:
        return False
    try:
        address = ipaddress.ip_address(remote_ip)
    except ValueError:
        return False
    if not isinstance(address, ipaddress.IPv4Address):
        return False
    return any(
        address in network
        for network in networks_by_cluster.get(cluster_id, ())
    )
```

Load clusters with:

```python
    clusters = session.scalars(
        select(Cluster)
        .options(selectinload(Cluster.internal_networks))
        .order_by(Cluster.name)
    ).all()
```

Initialize warnings before address-owner warnings:

```python
    warnings: list[str] = []
    networks_by_cluster = _cluster_network_map(clusters, warnings)
```

Extend warnings instead of replacing the list.

In the service loop, after computing `owners` and before choosing an external target:

```python
        if not owners and _is_cluster_internal_address(
            normalized_remote,
            source_device.cluster_id,
            networks_by_cluster,
        ):
            continue
```

This condition must be `not owners`, not `target_device is None`; multiple owners must retain their warning and external representation.

- [ ] **Step 5: Run basic filter tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py -k "internal_cidr or unclustered" -v
```

Expected: selected tests pass.

- [ ] **Step 6: Write managed-priority and IPv6 tests**

Add:

```python
def test_managed_cross_cluster_target_wins_over_internal_cidr(app):
    with app.state.session_factory() as session:
        source_cluster = Cluster(name="source")
        target_cluster = Cluster(name="target")
        set_internal_networks(source_cluster, "10.0.0.0/16")
        session.add_all([source_cluster, target_cluster])
        session.flush()
        source = add_device(session, app, "source-node", "192.168.1.1", source_cluster)
        target = add_device(session, app, "target-node", "10.0.0.20", target_cluster)
        add_scan(session, source, [target.host])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())
        pairs = {
            (edge["data"]["source"], edge["data"]["target"])
            for edge in topology["edges"]
        }

        assert (
            f"cluster-{source_cluster.id}",
            f"cluster-{target_cluster.id}",
        ) in pairs


def test_ambiguous_managed_address_is_not_hidden_by_internal_cidr(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="source")
        set_internal_networks(cluster, "10.0.0.0/16")
        session.add(cluster)
        session.flush()
        source = add_device(session, app, "source", "192.168.1.1", cluster)
        add_device(session, app, "owner-a", "10.0.0.20")
        add_device(session, app, "owner-b", "10.0.0.20")
        add_scan(session, source, ["10.0.0.20"])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())

        assert "external-10.0.0.20" in {
            node["data"]["id"] for node in topology["nodes"]
        }
        assert any("同时匹配多台设备" in warning for warning in topology["warnings"])


def test_ipv6_remote_is_not_filtered_by_ipv4_cidr(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="source")
        set_internal_networks(cluster, "10.0.0.0/8")
        session.add(cluster)
        session.flush()
        source = add_device(session, app, "source", "192.168.1.1", cluster)
        add_scan(session, source, ["2001:db8::8"])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())

        assert "external-2001:db8::8" in {
            node["data"]["id"] for node in topology["nodes"]
        }


def test_device_mode_keeps_remote_inside_cluster_cidr(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="source")
        set_internal_networks(cluster, "10.244.0.0/16")
        session.add(cluster)
        session.flush()
        source = add_device(session, app, "source", "192.168.1.1", cluster)
        scan = add_scan(session, source, ["10.244.2.8"])
        session.commit()

        topology = build_topology(scan)

        assert topology["edges"][0]["data"]["connections"][0]["remote_ip"] == (
            "10.244.2.8"
        )


def test_invalid_stored_cidr_is_ignored_with_warning(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="source")
        set_internal_networks(cluster, "not-a-cidr")
        session.add(cluster)
        session.flush()
        source = add_device(session, app, "source", "192.168.1.1", cluster)
        add_scan(session, source, ["203.0.113.8"])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())

        assert "external-203.0.113.8" in {
            node["data"]["id"] for node in topology["nodes"]
        }
        assert any("存在无效内部地址段" in warning for warning in topology["warnings"])
```

Update the topology import:

```python
from app.services.topology import build_cluster_topology, build_topology
```

- [ ] **Step 7: Parameterize time-window behavior**

Add:

```python
@pytest.mark.parametrize("window", ["current", "1d", "3d", "7d"])
def test_internal_cidr_applies_to_all_cluster_windows(app, window):
    with app.state.session_factory() as session:
        cluster = Cluster(name=f"k8s-{window}")
        set_internal_networks(cluster, "10.244.0.0/16")
        session.add(cluster)
        session.flush()
        source = add_device(session, app, "node", "192.168.1.10", cluster)
        add_scan(session, source, ["10.244.2.8"])
        session.commit()

        topology = build_cluster_topology(
            session,
            LiteralResolver(),
            window=window,
        )

        assert not topology["edges"]
```

Add `import pytest`.

- [ ] **Step 8: Run all topology tests and quality checkpoint**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_topology.py tests/test_topology_normalization.py -v
.\.venv\Scripts\python.exe -m ruff check app/services/topology.py tests/test_cluster_topology.py
```

Expected: all tests pass, including existing IP normalization, loopback, history and cluster-internal behavior.

---

### Task 5: 全量回归、浏览器验收与打包验证

**Files:**

- Verify: `app/`
- Verify: `tests/`
- Verify: `package-linux.ps1`
- Verify against: `docs/superpowers/specs/2026-07-30-cluster-internal-cidr-design.md`

**Interfaces:**

- Consumes complete implementation from Tasks 1–4.
- Produces verified local application and a timestamped Linux archive when packaging is requested during validation.

- [ ] **Step 1: Run full automated checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/js/topology.js
node --check app/static/js/clusters.js
```

Expected: all tests pass, Ruff prints `All checks passed!`, and both Node checks exit 0.

- [ ] **Step 2: Restart only this project's local service**

Resolve the process listening on port 8000, verify its command line contains `uvicorn app.main:app` and this workspace path, stop only that launcher/child pair, then start:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use `Start-Process -WindowStyle Hidden` and poll `/clusters` until it responds.

- [ ] **Step 3: Browser-test cluster CRUD**

At `http://127.0.0.1:8000/clusters`:

1. Confirm “集群管理” is a top-level active navigation item.
2. Create a cluster with `10.0.1.5/16` and `10.96.0.0/12`.
3. Confirm the page displays `10.0.0.0/16` and `10.96.0.0/12`.
4. Edit the description and replace networks with `172.16.0.0/12`.
5. Confirm the list refreshes with the new values.
6. Submit an IPv6 CIDR and confirm the Chinese validation error.
7. Delete the temporary cluster and confirm the warning text mentions ungrouping devices and deleting networks.

- [ ] **Step 4: Browser-test page responsibility split**

At `/devices`:

- “已有集群” management list is absent.
- Device cluster selection remains.
- Quick-create cluster remains.
- Cluster batch scan remains.
- Excel import remains.

- [ ] **Step 5: Browser-test topology filtering**

Create or use deterministic local test data containing:

- one source device in a cluster;
- an internal network such as `10.244.0.0/16`;
- one unmanaged remote inside that network;
- one unmanaged remote outside that network;
- one managed cross-cluster target whose IP is inside the configured network.

At `/topology` cluster mode:

- internal unmanaged remote is absent;
- external unmanaged remote is present;
- managed cross-cluster target is present;
- the result is consistent in current, 1d, 3d and 7d.

At device mode:

- the internal unmanaged remote remains visible.

- [ ] **Step 6: Check responsive layout and console**

At desktop and narrow width:

- form and list are side by side on wide screens and stack below 1100 px;
- CIDR tags wrap without horizontal overflow;
- controls remain keyboard accessible;
- there are no new console errors.

The existing Cytoscape custom `wheelSensitivity` warning is not a new failure.

- [ ] **Step 7: Verify timestamped Linux packaging**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
```

Expected:

- a new `connection-topology-linux-YYYYMMDD-HHmmss.tar.gz`;
- archive contains `app/models.py`, `app/templates/clusters.html`, `app/static/js/clusters.js`;
- archive remains readable with `tar -tzf`;
- older archives are not overwritten.

- [ ] **Step 8: Record the no-Git completion checkpoint**

Report:

- passing test count;
- Ruff and Node results;
- browser CRUD and topology-filter results;
- generated archive path if packaging was run;
- known unchanged warnings;
- modified files.

Do not stage, commit, branch, push, or create a pull request.
