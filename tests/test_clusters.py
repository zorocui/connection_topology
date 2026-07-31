import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Cluster, ClusterInternalNetwork, Device
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


def test_cluster_crud_and_quick_device_assignment(client, app, linux_device_payload):
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
    assert cluster["history_retention_days"] is None
    assert cluster["effective_history_retention_days"] == 7
    assert cluster["scan_interval_minutes"] == 5
    assert cluster["scheduled_enabled"] is True
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

    assert client.post("/api/clusters", json={"name": " 生产集群 "}).status_code == 409

    payload = {**linux_device_payload, "cluster_id": cluster["id"]}
    device_response = client.post("/api/devices", json=payload)
    assert device_response.status_code == 201
    assert device_response.json()["cluster_name"] == "生产集群"

    quick_payload = {
        **linux_device_payload,
        "host": "10.0.0.11",
        "new_cluster_name": "数据库集群",
    }
    quick = client.post("/api/devices", json=quick_payload)
    assert quick.status_code == 201
    assert quick.json()["cluster_name"] == "数据库集群"

    assert client.delete(f"/api/clusters/{cluster['id']}").status_code == 204
    with app.state.session_factory() as session:
        device = session.get(Device, device_response.json()["id"])
        assert device.cluster_id is None
        assert session.scalar(select(Cluster).where(Cluster.name == "数据库集群"))


def test_cluster_internal_network_update_supports_add_reorder_and_remove(client):
    created = client.post(
        "/api/clusters",
        json={
            "name": "地址段更新集群",
            "internal_networks": ["10.0.0.0/16", "10.96.0.0/12"],
        },
    ).json()
    endpoint = f"/api/clusters/{created['id']}"

    expanded = client.put(
        endpoint,
        json={
            "name": "地址段更新集群",
            "internal_networks": [
                "10.0.0.0/16",
                "10.96.0.0/12",
                "172.16.0.0/12",
            ],
        },
    )
    assert expanded.status_code == 200
    assert expanded.json()["internal_networks"] == [
        "10.0.0.0/16",
        "10.96.0.0/12",
        "172.16.0.0/12",
    ]

    reordered = client.put(
        endpoint,
        json={
            "name": "地址段更新集群",
            "internal_networks": [
                "172.16.0.0/12",
                "10.96.0.0/12",
                "10.0.0.0/16",
            ],
        },
    )
    assert reordered.status_code == 200
    assert reordered.json()["internal_networks"] == expanded.json()["internal_networks"]

    replaced = client.put(
        endpoint,
        json={
            "name": "地址段更新集群",
            "internal_networks": [
                "192.168.0.0/16",
                "10.0.0.0/16",
                "172.16.0.0/12",
            ],
        },
    )
    assert replaced.status_code == 200
    assert replaced.json()["internal_networks"] == [
        "10.0.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]


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


def test_cluster_scan_policy_updates_members_and_scheduler(
    client,
    app,
    linux_device_payload,
):
    cluster = client.post(
        "/api/clusters",
        json={"name": "scan-policy"},
    ).json()
    first = client.post(
        "/api/devices",
        json={
            **linux_device_payload,
            "cluster_id": cluster["id"],
            "scan_interval_minutes": 2,
        },
    ).json()
    second = client.post(
        "/api/devices",
        json={
            **linux_device_payload,
            "name": "second",
            "host": "10.0.0.11",
            "cluster_id": cluster["id"],
            "scan_interval_minutes": 3,
        },
    ).json()
    assert first["scan_interval_minutes"] == 5
    assert first["scheduled_enabled"] is True
    assert second["scan_interval_minutes"] == 5
    assert second["scheduled_enabled"] is True

    class SchedulerSpy:
        def __init__(self):
            self.device_ids = []

        def sync_device(self, device):
            self.device_ids.append(device.id)

        def shutdown(self):
            pass

    scheduler = SchedulerSpy()
    app.state.scheduler = scheduler
    response = client.put(
        f"/api/clusters/{cluster['id']}",
        json={
            "name": "scan-policy",
            "description": None,
            "internal_networks": [],
            "scan_interval_minutes": 12,
            "scheduled_enabled": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["scan_interval_minutes"] == 12
    assert response.json()["scheduled_enabled"] is False
    assert sorted(scheduler.device_ids) == sorted([first["id"], second["id"]])
    with app.state.session_factory() as session:
        members = session.scalars(
            select(Device).where(Device.cluster_id == cluster["id"])
        ).all()
        assert {
            (device.scan_interval_minutes, device.scheduled_enabled)
            for device in members
        } == {(12, False)}
