import time
from contextlib import contextmanager

import pytest

from app.collectors.base import CollectorError
from app.models import Device, OSType
from app.services.database_transactions import DatabaseUnavailable, TransactionConflict


def test_mutating_routes_enter_shared_transaction_guard(
    client,
    app,
    linux_device_payload,
    monkeypatch,
):
    names = []
    original = app.state.transaction_runner.guard

    @contextmanager
    def recording(name):
        names.append(name)
        with original(name):
            yield

    monkeypatch.setattr(app.state.transaction_runner, "guard", recording)
    assert client.post("/api/clusters", json={"name": "coordinated"}).status_code == 201
    assert client.post("/api/devices", json=linux_device_payload).status_code == 201
    assert client.put(
        "/api/settings",
        json={"history_retention_days": 11},
    ).status_code == 200
    assert {
        "api_create_cluster",
        "api_create_device",
        "api_update_settings",
    } <= set(names)


@pytest.mark.parametrize("error_type", [DatabaseUnavailable, TransactionConflict])
def test_mutating_route_maps_safe_database_errors_to_503(
    client,
    app,
    monkeypatch,
    error_type,
):
    @contextmanager
    def failing_guard(operation_name):
        raise error_type(operation_name)
        yield

    monkeypatch.setattr(app.state.transaction_runner, "guard", failing_guard)
    response = client.post("/api/clusters", json={"name": "unavailable"})
    assert response.status_code == 503
    assert response.json() == {"detail": str(error_type("api_create_cluster"))}


def seed_marker_device(app, *, host):
    with app.state.session_factory() as session:
        marker = Device(
            name="marker",
            host=host,
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt(""),
            collection_enabled=False,
        )
        session.add(marker)
        session.commit()
        return marker.id

def test_device_workflow_never_returns_password(client, linux_device_payload):
    response = client.post("/api/devices", json=linux_device_payload)
    assert response.status_code == 201
    body = response.json()
    assert "password" not in body
    assert "encrypted_password" not in body
    assert body["port"] == 22
    assert body["collection_enabled"] is True

    listed = client.get("/api/devices").json()
    assert listed[0]["name"] == "生产 Web 01"
    assert "encrypted_password" not in listed[0]
    assert listed[0]["collection_enabled"] is True


def test_valid_password_update_enables_marker_collection(client, app):
    marker_id = seed_marker_device(app, host="10.0.2.10")
    response = client.put(
        f"/api/devices/{marker_id}",
        json={"password": "working-password"},
    )
    assert response.status_code == 200
    assert response.json()["collection_enabled"] is True
    with app.state.session_factory() as session:
        device = session.get(Device, marker_id)
        assert device.collection_enabled is True
        assert app.state.cipher.decrypt(device.encrypted_password) == "working-password"


def test_failed_password_update_leaves_marker_disabled(client, app):
    marker_id = seed_marker_device(app, host="10.0.2.11")
    app.state.linux_collector.fail = CollectorError(
        "authentication_failed",
        "认证失败",
    )
    response = client.put(
        f"/api/devices/{marker_id}",
        json={"password": "bad-password"},
    )
    assert response.status_code == 502
    with app.state.session_factory() as session:
        device = session.get(Device, marker_id)
        assert device.collection_enabled is False
        assert app.state.cipher.decrypt(device.encrypted_password) == ""


def test_metadata_update_does_not_enable_marker(client, app):
    marker_id = seed_marker_device(app, host="10.0.2.12")
    response = client.put(
        f"/api/devices/{marker_id}",
        json={"name": "renamed-marker"},
    )
    assert response.status_code == 200
    assert response.json()["collection_enabled"] is False


def test_scan_and_topology_workflow(client, linux_device_payload):
    device = client.post("/api/devices", json=linux_device_payload).json()
    scan_response = client.post(f"/api/devices/{device['id']}/scan")
    assert scan_response.status_code == 202
    task_id = scan_response.json()["id"]
    for _ in range(50):
        task = client.get(f"/api/scan-tasks/{task_id}").json()
        if task["status"] in {"success", "failed"}:
            break
        time.sleep(0.02)
    assert task["status"] == "success"

    topology = client.get(f"/api/devices/{device['id']}/topology").json()
    assert len(topology["nodes"]) == 2
    assert topology["edges"][0]["data"]["count"] == 1
    assert topology["listeners"][0]["local_port"] == 22


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
    one_day = client.get(f"/api/devices/{device['id']}/topology?window=1d")
    invalid = client.get(f"/api/devices/{device['id']}/topology?window=30d")

    assert default.status_code == 200
    assert default.json()["window"] == "current"
    assert one_day.status_code == 200
    assert one_day.json()["window"] == "1d"
    assert invalid.status_code == 422


def test_cluster_topology_validates_time_window(client):
    response = client.get("/api/topology/clusters?window=3d")

    assert response.status_code == 200
    assert response.json()["window"] == "3d"
    assert client.get("/api/topology/clusters?window=30d").status_code == 422
    assert (
        client.get("/api/topology/clusters?cluster_id=999999").status_code
        == 404
    )


def test_settings_can_be_updated(client):
    response = client.put("/api/settings", json={"history_retention_days": 45})
    assert response.status_code == 200
    assert client.get("/api/settings").json()["history_retention_days"] == 45


def test_device_retention_inherits_cluster_and_system(
    client,
    linux_device_payload,
):
    cluster = client.post(
        "/api/clusters",
        json={
            "name": "retention-cluster",
            "history_retention_days": 14,
        },
    ).json()
    created = client.post(
        "/api/devices",
        json={**linux_device_payload, "cluster_id": cluster["id"]},
    )

    assert created.status_code == 201
    assert created.json()["history_retention_days"] is None
    assert created.json()["effective_history_retention_days"] == 14
    assert created.json()["history_retention_source"] == "cluster"

    device_id = created.json()["id"]
    overridden = client.put(
        f"/api/devices/{device_id}",
        json={"history_retention_days": 30},
    )
    assert overridden.status_code == 200
    assert overridden.json()["effective_history_retention_days"] == 30
    assert overridden.json()["history_retention_source"] == "device"

    inherited = client.put(
        f"/api/devices/{device_id}",
        json={"history_retention_days": None},
    )
    assert inherited.status_code == 200
    assert inherited.json()["effective_history_retention_days"] == 14
    assert inherited.json()["history_retention_source"] == "cluster"

    cluster_update = client.put(
        f"/api/clusters/{cluster['id']}",
        json={
            "name": "retention-cluster",
            "description": None,
            "internal_networks": [],
            "history_retention_days": None,
        },
    )
    assert cluster_update.status_code == 200
    assert cluster_update.json()["effective_history_retention_days"] == 7

    listed = client.get("/api/devices").json()
    assert listed[0]["effective_history_retention_days"] == 7
    assert listed[0]["history_retention_source"] == "system"


def test_retention_days_are_range_validated(client, linux_device_payload):
    assert client.post(
        "/api/clusters",
        json={"name": "invalid", "history_retention_days": 0},
    ).status_code == 422
    assert client.post(
        "/api/devices",
        json={**linux_device_payload, "history_retention_days": 3651},
    ).status_code == 422


def test_duplicate_device_is_conflict(client, linux_device_payload):
    assert client.post("/api/devices", json=linux_device_payload).status_code == 201
    assert client.post("/api/devices", json=linux_device_payload).status_code == 409
