# 设备列表搜索、分页与跳转 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将设备管理页的“已接入设备”改为服务端分页列表，并增加名称/IP搜索、页码按钮、每页数量切换和直接页码跳转。

**Architecture:** 新增独立设备列表查询服务，负责过滤、总数统计、稳定排序、页码修正和压缩页码窗口。`/devices` 页面路由只解析查询参数并渲染当前页；模板使用普通 GET 链接保证无 JavaScript 时仍可搜索和翻页，独立小型 JavaScript 文件负责每页数量切换和直接页码跳转。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、SQLite、Jinja2、原生 JavaScript、pytest、Ruff。

## Global Constraints

- 搜索仅匹配设备名称和主机地址/IP。
- 搜索只在点击“搜索”或按回车时执行，不自动搜索。
- 默认每页 20 台，可切换 20、50、100。
- 支持上一页、下一页、页码按钮和输入页码直接跳转。
- 查询状态使用 `q`、`page`、`page_size` 保存到 URL。
- 超范围页码重定向到最后一个有效页，URL 必须反映实际页码。
- 不修改现有 `/api/devices` 接口。
- 保持新增设备、扫描、查看拓扑、删除、Excel 导入和集群扫描功能。
- 所有新增用户可见文案使用中文。
- 不新增第三方依赖。
- 按用户要求不使用 Git；每项任务使用测试和静态检查作为检查点。

---

## File Structure

- Create: `app/services/device_listing.py`
  - 设备搜索、分页、显示范围和压缩页码窗口。
- Modify: `app/routes/pages.py`
  - 接收 `/devices` 查询参数、调用分页服务并规范化超范围 URL。
- Modify: `app/templates/devices.html`
  - 增加搜索工具栏、总数说明、分页控件和直接跳转控件。
- Create: `app/static/js/device-list.js`
  - 每页数量切换和页码输入跳转。
- Modify: `app/static/css/app.css`
  - 设备列表工具栏、分页和窄屏样式。
- Create: `tests/test_device_listing.py`
  - 验证数据库级搜索、分页和页码窗口。
- Modify: `tests/test_pages.py`
  - 验证页面参数、当前页行数、链接状态和超范围重定向。
- Create: `tests/test_device_list_frontend.py`
  - 验证独立页面脚本契约。

---

### Task 1: 设备列表查询服务

**Files:**

- Create: `app/services/device_listing.py`
- Create: `tests/test_device_listing.py`

**Interfaces:**

- Produces: `ALLOWED_PAGE_SIZES: frozenset[int]`.
- Produces: `DevicePage`.
- Produces: `build_page_links(page: int, total_pages: int) -> list[int | None]`.
- Produces: `list_device_page(session: Session, query: str, page: int, page_size: int) -> DevicePage`.

- [ ] **Step 1: Write failing service tests**

Create `tests/test_device_listing.py`:

```python
from app.models import Cluster, Device, OSType
from app.services.device_listing import (
    build_page_links,
    list_device_page,
)


def add_device(
    session,
    app,
    *,
    name,
    host,
    username="tester",
    cluster=None,
):
    device = Device(
        name=name,
        host=host,
        os_type=OSType.LINUX,
        port=22,
        username=username,
        encrypted_password=app.state.cipher.encrypt("secret"),
        cluster_id=cluster.id if cluster else None,
        scheduled_enabled=False,
    )
    session.add(device)
    return device


def add_numbered_devices(session, app, count):
    for index in range(count):
        add_device(
            session,
            app,
            name=f"device-{index:03d}",
            host=f"10.20.{index // 250}.{index % 250 + 1}",
        )
    session.commit()


def test_device_page_reads_only_requested_slice(app):
    with app.state.session_factory() as session:
        add_numbered_devices(session, app, 45)

        first = list_device_page(session, "", 1, 20)
        second = list_device_page(session, "", 2, 20)
        third = list_device_page(session, "", 3, 20)

        assert [len(first.items), len(second.items), len(third.items)] == [
            20,
            20,
            5,
        ]
        assert first.items[0].name == "device-000"
        assert second.items[0].name == "device-020"
        assert third.items[-1].name == "device-044"
        assert third.total_items == 45
        assert third.total_pages == 3
        assert (third.first_item, third.last_item) == (41, 45)


def test_device_search_matches_only_name_and_host(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="needle-cluster")
        session.add(cluster)
        session.flush()
        add_device(
            session,
            app,
            name="needle-name",
            host="192.0.2.10",
        )
        add_device(
            session,
            app,
            name="username-only",
            host="192.0.2.20",
            username="needle-user",
        )
        add_device(
            session,
            app,
            name="cluster-only",
            host="198.51.100.20",
            cluster=cluster,
        )
        session.commit()

        by_name = list_device_page(session, " needle ", 1, 20)
        by_host = list_device_page(session, "198.51.100", 1, 20)

        assert [device.name for device in by_name.items] == ["needle-name"]
        assert [device.name for device in by_host.items] == ["cluster-only"]
        assert by_name.query == "needle"


def test_device_search_treats_sql_wildcards_as_literals(app):
    with app.state.session_factory() as session:
        add_device(
            session,
            app,
            name="literal%device",
            host="192.0.2.1",
        )
        add_device(
            session,
            app,
            name="literalXdevice",
            host="192.0.2.2",
        )
        add_device(
            session,
            app,
            name="literal_device",
            host="192.0.2.3",
        )
        session.commit()

        percent = list_device_page(session, "%", 1, 20)
        underscore = list_device_page(session, "_", 1, 20)

        assert [device.name for device in percent.items] == ["literal%device"]
        assert [device.name for device in underscore.items] == [
            "literal_device"
        ]


def test_device_page_clamps_page_and_handles_empty_results(app):
    with app.state.session_factory() as session:
        add_numbered_devices(session, app, 25)

        clamped = list_device_page(session, "", 99, 20)
        empty = list_device_page(session, "not-found", 9, 20)

        assert clamped.page == 2
        assert len(clamped.items) == 5
        assert empty.page == 1
        assert empty.total_pages == 0
        assert empty.items == []
        assert (empty.first_item, empty.last_item) == (0, 0)
        assert empty.page_links == []


def test_device_page_rejects_invalid_arguments(app):
    with app.state.session_factory() as session:
        for page, page_size, message in [
            (0, 20, "页码必须大于等于 1"),
            (1, 30, "每页数量仅支持 20、50、100"),
        ]:
            try:
                list_device_page(session, "", page, page_size)
            except ValueError as exc:
                assert str(exc) == message
            else:
                raise AssertionError("无效分页参数未被拒绝")


def test_build_page_links_compresses_large_ranges():
    assert build_page_links(1, 3) == [1, 2, 3]
    assert build_page_links(1, 100) == [1, 2, 3, None, 100]
    assert build_page_links(50, 100) == [
        1,
        None,
        48,
        49,
        50,
        51,
        52,
        None,
        100,
    ]
    assert build_page_links(100, 100) == [
        1,
        None,
        98,
        99,
        100,
    ]
```

- [ ] **Step 2: Run service tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_device_listing.py -v
```

Expected: collection fails because `app.services.device_listing` does not exist.

- [ ] **Step 3: Implement the query service**

Create `app/services/device_listing.py`:

```python
from dataclasses import dataclass
from math import ceil

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Device

ALLOWED_PAGE_SIZES = frozenset({20, 50, 100})


@dataclass(frozen=True)
class DevicePage:
    items: list[Device]
    query: str
    page: int
    page_size: int
    total_items: int
    total_pages: int
    first_item: int
    last_item: int
    page_links: list[int | None]


def build_page_links(page: int, total_pages: int) -> list[int | None]:
    if total_pages <= 0:
        return []
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    visible = {
        1,
        total_pages,
        *range(max(2, page - 2), min(total_pages, page + 2) + 1),
    }
    links: list[int | None] = []
    previous = 0
    for current in sorted(visible):
        if previous and current - previous > 1:
            links.append(None)
        links.append(current)
        previous = current
    return links


def list_device_page(
    session: Session,
    query: str,
    page: int,
    page_size: int,
) -> DevicePage:
    if page < 1:
        raise ValueError("页码必须大于等于 1")
    if page_size not in ALLOWED_PAGE_SIZES:
        raise ValueError("每页数量仅支持 20、50、100")

    normalized_query = query.strip()
    filters = []
    if normalized_query:
        filters.append(
            or_(
                Device.name.contains(normalized_query, autoescape=True),
                Device.host.contains(normalized_query, autoescape=True),
            )
        )

    count_statement = select(func.count()).select_from(Device)
    if filters:
        count_statement = count_statement.where(*filters)
    total_items = session.scalar(count_statement) or 0
    total_pages = ceil(total_items / page_size) if total_items else 0
    resolved_page = min(page, total_pages) if total_pages else 1

    statement = (
        select(Device)
        .options(selectinload(Device.cluster))
        .order_by(Device.name, Device.id)
        .offset((resolved_page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        statement = statement.where(*filters)
    items = list(session.scalars(statement).all())
    first_item = (resolved_page - 1) * page_size + 1 if items else 0
    last_item = first_item + len(items) - 1 if items else 0

    return DevicePage(
        items=items,
        query=normalized_query,
        page=resolved_page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        first_item=first_item,
        last_item=last_item,
        page_links=build_page_links(resolved_page, total_pages),
    )
```

- [ ] **Step 4: Run service tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_device_listing.py -v
.\.venv\Scripts\python.exe -m ruff check app/services/device_listing.py tests/test_device_listing.py
```

Expected: six tests pass and Ruff prints `All checks passed!`.

---

### Task 2: 页面路由、搜索表单和服务端分页链接

**Files:**

- Modify: `app/routes/pages.py`
- Modify: `app/templates/devices.html`
- Modify: `tests/test_pages.py`

**Interfaces:**

- Consumes: `list_device_page(session, query, page, page_size) -> DevicePage`.
- Produces: `/devices?q=<text>&page=<int>&page_size=<20|50|100>`.
- Produces template context: `devices`, `clusters`, `device_page`.
- Produces DOM IDs: `device-search-form`, `device-search`, `device-page-size`, `device-page-jump-input`, `device-page-jump-button`.

- [ ] **Step 1: Add failing route and rendering tests**

Extend imports in `tests/test_pages.py`:

```python
from urllib.parse import parse_qs, urlparse

from app.models import Device, OSType, ScanRun, ScanStatus, ScanTrigger
```

Add a helper:

```python
def add_page_devices(session, app, count):
    for index in range(count):
        session.add(
            Device(
                name=f"page-device-{index:03d}",
                host=f"10.30.{index // 250}.{index % 250 + 1}",
                os_type=OSType.LINUX,
                port=22,
                username="page-test",
                encrypted_password=app.state.cipher.encrypt("secret"),
                scheduled_enabled=False,
            )
        )
    session.commit()
```

Add:

```python
def test_devices_page_paginates_and_preserves_query_state(client, app):
    with app.state.session_factory() as session:
        add_page_devices(session, app, 45)

    first = client.get("/devices")
    second = client.get(
        "/devices?q=page-device&page=2&page_size=20"
    )
    third = client.get(
        "/devices?q=page-device&page=3&page_size=20"
    )

    assert first.text.count("data-device-row") == 20
    assert second.text.count("data-device-row") == 20
    assert third.text.count("data-device-row") == 5
    assert "page-device-020" in second.text
    assert "page-device-000" not in second.text
    assert 'id="device-total-count" class="count-badge">45<' in second.text
    assert "共 45 台设备，当前显示 21–40" in second.text
    assert (
        "/devices?q=page-device&amp;page=3&amp;page_size=20"
        in second.text
    )
    assert 'aria-current="page"' in second.text


def test_devices_page_searches_name_and_host(client, app):
    with app.state.session_factory() as session:
        add_page_devices(session, app, 25)

    by_name = client.get("/devices?q=page-device-024")
    by_host = client.get("/devices?q=10.30.0.25")
    missing = client.get("/devices?q=page-test")

    assert "page-device-024" in by_name.text
    assert by_name.text.count("data-device-row") == 1
    assert "page-device-024" in by_host.text
    assert "未找到匹配的设备" in missing.text
    assert 'href="/devices?page=1&amp;page_size=20"' in missing.text


def test_devices_page_redirects_out_of_range_page(client, app):
    with app.state.session_factory() as session:
        add_page_devices(session, app, 21)

    response = client.get(
        "/devices?q=page-device&page=99&page_size=20",
        follow_redirects=False,
    )

    assert response.status_code == 303
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/devices"
    assert parse_qs(parsed.query) == {
        "q": ["page-device"],
        "page": ["2"],
        "page_size": ["20"],
    }

    trimmed = client.get(
        "/devices?q=%20page-device%20&page=1&page_size=20",
        follow_redirects=False,
    )
    assert trimmed.status_code == 303
    trimmed_query = parse_qs(urlparse(trimmed.headers["location"]).query)
    assert trimmed_query == {
        "q": ["page-device"],
        "page": ["1"],
        "page_size": ["20"],
    }


def test_devices_page_validates_page_parameters(client):
    assert client.get("/devices?page=0").status_code == 422
    assert client.get("/devices?page_size=30").status_code == 422
```

- [ ] **Step 2: Run route tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py -k "devices_page" -v
```

Expected: new assertions fail because `/devices` still renders all devices and has no pagination controls.

- [ ] **Step 3: Implement route query parameters and canonical redirect**

In `app/routes/pages.py`, add imports:

```python
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.device_listing import ALLOWED_PAGE_SIZES, list_device_page
```

Replace `devices_page`:

```python
@router.get("/devices", response_class=HTMLResponse)
def devices_page(
    request: Request,
    q: str = Query(default="", max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
):
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=422,
            detail="每页数量仅支持 20、50、100",
        )
    device_page = list_device_page(db, q, page, page_size)
    if page != device_page.page or q != device_page.query:
        params = {
            "q": device_page.query,
            "page": device_page.page,
            "page_size": device_page.page_size,
        }
        if not device_page.query:
            params.pop("q")
        return RedirectResponse(
            url=f"/devices?{urlencode(params)}",
            status_code=303,
        )
    context = _base_context(request, "devices")
    context["devices"] = device_page.items
    context["device_page"] = device_page
    context["clusters"] = db.scalars(
        select(Cluster).order_by(Cluster.name)
    ).all()
    return templates.TemplateResponse(request, "devices.html", context)
```

- [ ] **Step 4: Add the search toolbar and result summary**

In `app/templates/devices.html`, change the count badge:

```html
<span id="device-total-count" class="count-badge">{{ device_page.total_items }}</span>
```

Insert after that module's `.panel-head`:

```html
<form id="device-search-form" class="device-list-toolbar" method="get" action="/devices">
  <input name="page" type="hidden" value="1">
  <label class="device-list-search">搜索设备
    <input id="device-search" name="q" type="search"
      value="{{ device_page.query }}"
      placeholder="搜索设备名称或主机地址">
  </label>
  <label>每页显示
    <select id="device-page-size" name="page_size">
      {% for size in [20, 50, 100] %}
      <option value="{{ size }}" {{ "selected" if size == device_page.page_size else "" }}>
        {{ size }} 台
      </option>
      {% endfor %}
    </select>
  </label>
  <button class="button primary" type="submit">搜索</button>
  {% if device_page.query %}
  <a class="button" href="/devices?page=1&amp;page_size={{ device_page.page_size }}">
    清除
  </a>
  {% endif %}
  <p class="device-list-summary">
    {% if device_page.total_items %}
      共 {{ device_page.total_items }} 台设备，当前显示 {{ device_page.first_item }}–{{ device_page.last_item }}
    {% elif device_page.query %}
      未找到匹配的设备
    {% else %}
      尚未接入设备
    {% endif %}
  </p>
</form>
```

- [ ] **Step 5: Mark rows and distinguish empty states**

Change each device row:

```html
<tr data-device-row data-device-id="{{ device.id }}">
```

Replace the existing empty branch:

```html
{% else %}
<tr>
  <td colspan="5" class="muted">
    {{ "未找到匹配的设备" if device_page.query else "尚未接入设备" }}
  </td>
</tr>
{% endfor %}
```

- [ ] **Step 6: Add server-rendered pagination controls**

Insert immediately after `.table-wrap`:

```html
{% if device_page.total_pages > 1 %}
<nav class="device-pagination" aria-label="设备列表分页">
  {% if device_page.page > 1 %}
  <a class="page-button"
    href="/devices?q={{ device_page.query|urlencode }}&amp;page={{ device_page.page - 1 }}&amp;page_size={{ device_page.page_size }}">
    上一页
  </a>
  {% else %}
  <span class="page-button disabled">上一页</span>
  {% endif %}

  <div class="page-numbers">
    {% for page_number in device_page.page_links %}
      {% if page_number is none %}
      <span class="page-ellipsis" aria-hidden="true">…</span>
      {% elif page_number == device_page.page %}
      <button class="page-button active" type="button"
        aria-current="page" disabled>{{ page_number }}</button>
      {% else %}
      <a class="page-button"
        href="/devices?q={{ device_page.query|urlencode }}&amp;page={{ page_number }}&amp;page_size={{ device_page.page_size }}">
        {{ page_number }}
      </a>
      {% endif %}
    {% endfor %}
  </div>

  {% if device_page.page < device_page.total_pages %}
  <a class="page-button"
    href="/devices?q={{ device_page.query|urlencode }}&amp;page={{ device_page.page + 1 }}&amp;page_size={{ device_page.page_size }}">
    下一页
  </a>
  {% else %}
  <span class="page-button disabled">下一页</span>
  {% endif %}

  <div id="device-page-jump" class="page-jump"
    data-total-pages="{{ device_page.total_pages }}">
    <label for="device-page-jump-input">跳转到</label>
    <input id="device-page-jump-input" type="number" min="1"
      max="{{ device_page.total_pages }}" value="{{ device_page.page }}">
    <button id="device-page-jump-button" class="button" type="button">跳转</button>
  </div>
</nav>
{% endif %}
```

- [ ] **Step 7: Run page tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pages.py tests/test_device_listing.py -v
.\.venv\Scripts\python.exe -m ruff check app/routes/pages.py app/services/device_listing.py tests/test_pages.py tests/test_device_listing.py
```

Expected: all selected tests pass and Ruff passes.

---

### Task 3: 页容量切换、直接跳转和响应式样式

**Files:**

- Create: `app/static/js/device-list.js`
- Modify: `app/templates/devices.html`
- Modify: `app/static/css/app.css`
- Create: `tests/test_device_list_frontend.py`

**Interfaces:**

- Consumes DOM IDs from Task 2.
- Produces URL navigation with `q`, `page`, `page_size`.
- Keeps search execution tied to the GET form submit.

- [ ] **Step 1: Write failing frontend contract tests**

Create `tests/test_device_list_frontend.py`:

```python
from pathlib import Path

DEVICE_LIST_JS = Path("app/static/js/device-list.js")


def script_text() -> str:
    return DEVICE_LIST_JS.read_text(encoding="utf-8")


def test_page_size_change_preserves_query_and_resets_page():
    script = script_text()

    assert 'getElementById("device-page-size")' in script
    assert 'url.searchParams.set("page_size", pageSize.value)' in script
    assert 'url.searchParams.set("page", "1")' in script
    assert "window.location.assign(url)" in script


def test_direct_page_jump_clamps_and_supports_enter():
    script = script_text()

    assert 'getElementById("device-page-jump-input")' in script
    assert 'getElementById("device-page-jump-button")' in script
    assert "Math.min(Math.max(requestedPage, 1), totalPages)" in script
    assert 'event.key !== "Enter"' in script
    assert 'toast("请输入有效页码", "error")' in script
```

- [ ] **Step 2: Run frontend tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_device_list_frontend.py -v
```

Expected: tests fail because `device-list.js` does not exist.

- [ ] **Step 3: Implement the isolated device-list script**

Create `app/static/js/device-list.js`:

```javascript
(() => {
  const pageSize = document.getElementById("device-page-size");
  const jump = document.getElementById("device-page-jump");
  const jumpInput = document.getElementById("device-page-jump-input");
  const jumpButton = document.getElementById("device-page-jump-button");

  const navigateToPage = page => {
    const url = new URL(window.location.href);
    url.searchParams.set("page", String(page));
    window.location.assign(url);
  };

  pageSize?.addEventListener("change", () => {
    const url = new URL(window.location.href);
    url.searchParams.set("page_size", pageSize.value);
    url.searchParams.set("page", "1");
    window.location.assign(url);
  });

  const goToRequestedPage = () => {
    const rawValue = jumpInput?.value.trim() || "";
    if (!/^\d+$/.test(rawValue)) {
      return toast("请输入有效页码", "error");
    }
    const totalPages = Number(jump.dataset.totalPages);
    const requestedPage = Number(rawValue);
    const targetPage = Math.min(Math.max(requestedPage, 1), totalPages);
    navigateToPage(targetPage);
  };

  jumpButton?.addEventListener("click", goToRequestedPage);
  jumpInput?.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    goToRequestedPage();
  });
})();
```

- [ ] **Step 4: Load the new script from the device page**

At the end of `app/templates/devices.html`, after the existing inline
`</script>` and before `{% endblock %}`, add:

```html
<script src="{{ url_for('static', path='js/device-list.js') }}"></script>
```

- [ ] **Step 5: Add device-list styles**

Append before the existing media queries in `app/static/css/app.css`:

```css
.device-list-toolbar {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 10px;
  padding: 16px 22px;
  border-bottom: 1px solid var(--line);
}
.device-list-search { flex: 1 1 260px; }
.device-list-summary {
  flex: 1 1 100%;
  margin: 2px 0 0;
  color: var(--muted);
  font: 10px var(--mono);
}
.device-pagination {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 16px 22px;
  border-top: 1px solid var(--line);
}
.page-numbers { display: flex; flex-wrap: wrap; gap: 6px; }
.page-button {
  min-width: 34px;
  min-height: 34px;
  display: inline-grid;
  place-items: center;
  padding: 7px 10px;
  border: 1px solid var(--line-bright);
  background: transparent;
  color: var(--text);
  font: 10px var(--mono);
}
.page-button:hover { border-color: var(--signal); color: var(--signal); }
.page-button.active {
  border-color: var(--signal);
  background: var(--signal);
  color: var(--ink);
}
.page-button.disabled { opacity: .42; cursor: not-allowed; }
.page-ellipsis {
  min-width: 22px;
  display: inline-grid;
  place-items: center;
  color: var(--muted);
}
.page-jump {
  margin-left: auto;
  display: flex;
  align-items: end;
  gap: 8px;
}
.page-jump label { display: block; }
.page-jump input { width: 74px; }
```

Inside `@media (max-width: 760px)` add:

```css
  .device-list-toolbar { align-items: stretch; }
  .device-list-toolbar > .button,
  .device-list-toolbar > label { flex: 1 1 100%; }
  .device-pagination { align-items: stretch; }
  .page-jump { width: 100%; margin-left: 0; }
  .page-jump input { flex: 1; }
```

- [ ] **Step 6: Run frontend and page checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_device_list_frontend.py tests/test_pages.py -v
node --check app/static/js/device-list.js
.\.venv\Scripts\python.exe -m ruff check tests/test_device_list_frontend.py tests/test_pages.py
```

Expected: tests pass, Node exits 0, and Ruff passes.

---

### Task 4: 全量回归、浏览器验收和部署包

**Files:**

- Verify: `app/`
- Verify: `tests/`
- Verify: `package-linux.ps1`
- Verify against: `docs/superpowers/specs/2026-07-30-device-list-pagination-design.md`

**Interfaces:**

- Consumes complete implementation from Tasks 1–3.
- Produces verified local service and a new timestamped Linux archive.

- [ ] **Step 1: Run all automated checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/js/device-list.js
node --check app/static/js/topology.js
node --check app/static/js/clusters.js
```

Expected: all tests pass, Ruff prints `All checks passed!`, and all Node
commands exit 0.

- [ ] **Step 2: Restart only this workspace's service**

Resolve the process listening on port 8000. Verify its launcher command contains
both this workspace path and `uvicorn app.main:app`; stop only that launcher and
its verified child process. Start:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use `Start-Process -WindowStyle Hidden`, then poll
`http://127.0.0.1:8000/devices` until it returns 200.

- [ ] **Step 3: Insert isolated browser-acceptance devices**

Run:

```powershell
@'
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from app.models import Device, OSType

engine = create_engine("sqlite:///./connection_topology.db")
with Session(engine) as session:
    session.execute(
        delete(Device).where(
            Device.name.startswith("__pagination_acceptance_")
        )
    )
    for index in range(41):
        session.add(
            Device(
                name=f"__pagination_acceptance_{index:03d}__",
                host=f"198.18.1.{index + 1}",
                os_type=OSType.LINUX,
                port=22,
                username="pagination-acceptance",
                encrypted_password="temporary",
                scheduled_enabled=False,
            )
        )
    session.commit()
    print("CREATED=41")
'@ | .\.venv\Scripts\python.exe -
```

Expected: `CREATED=41`.

- [ ] **Step 4: Browser-test search and page state**

At:

```text
http://127.0.0.1:8000/devices?q=__pagination_acceptance_&page=1&page_size=20
```

Verify:

1. The total is 41 and current range is 1–20.
2. Exactly 20 device rows are rendered.
3. Clicking page 2 updates the URL and shows 21–40.
4. Clicking “下一页” shows page 3 with one device.
5. Searching `198.18.1.38` by button returns only device 037.
6. Enter in the search field performs the same GET search.
7. “清除” removes `q` and returns page 1.

- [ ] **Step 5: Browser-test page size and direct jump**

Using the same acceptance search prefix:

1. Change page size from 20 to 50.
2. Confirm the URL contains `page_size=50&page=1`.
3. Confirm all 41 matching devices appear on one page.
4. Change back to 20.
5. Enter page `3` and click “跳转”; confirm page 3.
6. Enter page `999`; confirm the browser navigates to page 3.
7. Clear the input and click “跳转”; confirm the Chinese error
   “请输入有效页码”.

- [ ] **Step 6: Browser-test deletion clamp and responsive layout**

With the acceptance prefix at page 3:

1. Delete its only device and accept the confirmation.
2. Confirm the response redirects to page 2 and the URL now contains `page=2`.
3. Set a viewport narrower than 760 px.
4. Confirm search controls and pagination wrap vertically.
5. Confirm there is no horizontal document overflow.
6. Reset the viewport.
7. Confirm the browser console has no new errors.

The existing Cytoscape custom `wheelSensitivity` warning is not a new failure.

- [ ] **Step 7: Remove all browser-acceptance devices**

Run:

```powershell
@'
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session
from app.models import Device

engine = create_engine("sqlite:///./connection_topology.db")
with Session(engine) as session:
    session.execute(
        delete(Device).where(
            Device.name.startswith("__pagination_acceptance_")
        )
    )
    session.commit()
    remaining = session.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.name.startswith("__pagination_acceptance_"))
    )
    print(f"REMAINING={remaining}")
'@ | .\.venv\Scripts\python.exe -
```

Expected: `REMAINING=0`. Do not delete or modify any other device.

- [ ] **Step 8: Generate and inspect a timestamped Linux package**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-linux.ps1
```

Verify the newest archive:

```powershell
$archive = Get-ChildItem -Filter 'connection-topology-linux-*.tar.gz' |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
tar -tzf $archive.FullName | Select-String -Pattern `
  '^app/services/device_listing.py$|^app/static/js/device-list.js$|^app/templates/devices.html$'
```

Expected:

- a new `connection-topology-linux-YYYYMMDD-HHmmss.tar.gz`;
- all three changed runtime files are present;
- `tar -tzf` exits 0;
- older archives remain unchanged.

- [ ] **Step 9: Record the no-Git completion checkpoint**

Report:

- passing test count;
- Ruff and Node results;
- browser search, pagination, direct-jump, deletion-clamp and responsive results;
- restarted service URL;
- generated archive path;
- known unchanged warnings;
- modified and created files.

Do not stage, commit, branch, push, or create a pull request.
