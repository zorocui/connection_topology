from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.models import Device, OSType, ScanRun, ScanStatus, ScanTrigger


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


@pytest.mark.parametrize(
    "path",
    ["/", "/topology", "/devices", "/clusters", "/history", "/settings"],
)
def test_page_routes_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "连接图谱" in response.text


def test_devices_page_contains_secure_form(client):
    response = client.get("/devices")
    assert "凭据加密保存" in response.text
    assert 'type="password"' in response.text
    assert "Excel 批量导入" in response.text
    assert "下载导入模板" in response.text
    assert "所属集群" in response.text
    assert "批量扫描" in response.text
    assert "扫描全部设备" in response.text
    assert "扫描所选集群" in response.text
    assert "执行中" in response.text


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


def test_topology_page_contains_mode_switch(client):
    response = client.get("/topology")
    assert "设备模式" in response.text
    assert "集群模式" in response.text


def test_topology_device_filter_excludes_annotation_only_devices(client, app):
    with app.state.session_factory() as session:
        session.add_all(
            [
                Device(
                    name="collectable-target",
                    host="10.40.0.1",
                    os_type=OSType.LINUX,
                    port=22,
                    username="collector",
                    encrypted_password=app.state.cipher.encrypt("secret"),
                    scheduled_enabled=False,
                    collection_enabled=True,
                ),
                Device(
                    name="annotation-only-target",
                    host="10.40.0.2",
                    os_type=OSType.LINUX,
                    port=22,
                    username="",
                    encrypted_password="",
                    scheduled_enabled=False,
                    collection_enabled=False,
                ),
            ]
        )
        session.commit()

    response = client.get("/topology")
    device_select = response.text.split('<select id="device-filter">', 1)[1].split(
        "</select>", 1
    )[0]

    assert "collectable-target" in device_select
    assert "annotation-only-target" not in device_select
    assert 'id="cluster-filter"' in response.text


def test_topology_page_contains_canvas_view_controls(client):
    response = client.get("/topology")

    assert response.text.count('id="fit-topology-button"') == 1
    assert response.text.count('id="reset-topology-button"') == 1
    assert "适配全图" in response.text
    assert "重置视图" in response.text


def test_topology_page_contains_time_window_and_history_legend(client):
    response = client.get("/topology")

    assert response.status_code == 200
    assert response.text.count('id="topology-window"') == 1
    assert '<option value="current">当前</option>' in response.text
    assert '<option value="1d">1d</option>' in response.text
    assert '<option value="3d">3d</option>' in response.text
    assert '<option value="7d">7d</option>' in response.text
    assert "已断开连接" in response.text


def test_history_device_column_uses_device_name(client, app):
    with app.state.session_factory() as session:
        device = Device(
            name="history-device-name",
            host="10.0.0.91",
            os_type=OSType.LINUX,
            port=22,
            username="tester",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(device)
        session.flush()
        device_id = device.id
        session.add(
            ScanRun(
                device_id=device_id,
                trigger_type=ScanTrigger.MANUAL,
                status=ScanStatus.SUCCESS,
                connection_count=0,
            )
        )
        session.commit()

    response = client.get("/history")
    history_table = response.text.split('<table id="history-table">', 1)[1].split(
        "</table>", 1
    )[0]

    assert "<td>history-device-name</td>" in history_table
    assert f"<td>设备 {device_id}</td>" not in history_table


def test_pages_render_all_visible_times_in_beijing(client, app):
    with app.state.session_factory() as session:
        device = Device(
            name="timezone-device",
            host="10.0.0.90",
            os_type=OSType.LINUX,
            port=22,
            username="tester",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(device)
        session.flush()
        session.add(
            ScanRun(
                device_id=device.id,
                trigger_type=ScanTrigger.MANUAL,
                status=ScanStatus.SUCCESS,
                started_at=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 7, 29, 1, 1, tzinfo=timezone.utc),
                connection_count=0,
            )
        )
        session.commit()

    history = client.get("/history")
    dashboard = client.get("/")

    assert "2026-07-29 09:00:00" in history.text
    assert "07-29 09:00:00" in dashboard.text
    assert 'timeZone: "Asia/Shanghai"' in dashboard.text
