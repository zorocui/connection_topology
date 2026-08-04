from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Cluster,
    ClusterInternalNetwork,
    ConnectionRecord,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.topology import build_cluster_topology, build_topology


class LiteralResolver:
    def resolve(self, host: str) -> set[str]:
        return {host}


def set_internal_networks(cluster, *cidrs):
    cluster.internal_networks = [
        ClusterInternalNetwork(cidr=cidr) for cidr in cidrs
    ]


def add_device(session, app, name, host, cluster=None):
    device = Device(
        name=name,
        host=host,
        os_type=OSType.LINUX,
        port=22,
        username=name.lower(),
        encrypted_password=app.state.cipher.encrypt("secret"),
        cluster_id=cluster.id if cluster else None,
    )
    session.add(device)
    session.flush()
    return device


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


def test_cluster_topology_hides_internal_and_aggregates_external(app):
    with app.state.session_factory() as session:
        production = Cluster(name="生产集群")
        database = Cluster(name="数据库集群")
        session.add_all([production, database])
        session.flush()
        web1 = add_device(session, app, "web1", "10.0.0.1", production)
        web2 = add_device(session, app, "web2", "10.0.0.2", production)
        db = add_device(session, app, "db1", "10.0.1.1", database)
        standalone = add_device(session, app, "standalone", "10.0.2.1")
        add_scan(session, web1, ["10.0.0.2", "10.0.1.1", "203.0.113.8"])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())
        node_ids = {node["data"]["id"] for node in topology["nodes"]}
        assert f"cluster-{production.id}" in node_ids
        assert f"cluster-{database.id}" in node_ids
        assert f"device-{standalone.id}" in node_ids
        assert "external-203.0.113.8" in node_ids
        pairs = {
            (edge["data"]["source"], edge["data"]["target"])
            for edge in topology["edges"]
        }
        assert (f"cluster-{production.id}", f"cluster-{database.id}") in pairs
        assert (f"cluster-{production.id}", "external-203.0.113.8") in pairs
        assert all(source != target for source, target in pairs)
        assert not any(
            detail["remote_ip"] == web2.host
            for edge in topology["edges"]
            for detail in edge["data"]["connections"]
        )
        assert db.id


def test_cluster_topology_returns_only_target_one_hop(app):
    with app.state.session_factory() as session:
        selected = Cluster(name="目标集群")
        peer = Cluster(name="对端集群")
        unrelated = Cluster(name="无关集群")
        session.add_all([selected, peer, unrelated])
        session.flush()
        source = add_device(session, app, "source", "10.0.0.1", selected)
        target = add_device(session, app, "target", "10.0.1.1", peer)
        other = add_device(session, app, "other", "10.0.2.1", unrelated)
        add_scan(session, source, [target.host, "203.0.113.8"])
        add_scan(session, other, ["198.51.100.9"])
        session.commit()

        topology = build_cluster_topology(
            session,
            LiteralResolver(),
            target_cluster_id=selected.id,
        )

        assert {node["data"]["id"] for node in topology["nodes"]} == {
            f"cluster-{selected.id}",
            f"cluster-{peer.id}",
            "external-203.0.113.8",
        }
        assert {
            (edge["data"]["source"], edge["data"]["target"])
            for edge in topology["edges"]
        } == {
            (f"cluster-{selected.id}", f"cluster-{peer.id}"),
            (f"cluster-{selected.id}", "external-203.0.113.8"),
        }


def test_current_target_cluster_keeps_only_outbound_and_inbound_one_hop(app):
    with app.state.session_factory() as session:
        selected = Cluster(name="当前目标集群")
        peer = Cluster(name="当前对端集群")
        unrelated = Cluster(name="当前无关集群")
        session.add_all([selected, peer, unrelated])
        session.flush()
        source = add_device(session, app, "source-current", "10.0.0.1", selected)
        inbound = add_device(session, app, "inbound-current", "10.0.1.1", peer)
        other = add_device(session, app, "other-current", "10.0.2.1", unrelated)
        add_scan(session, source, [inbound.host, "203.0.113.8"])
        add_scan(session, inbound, [source.host])
        add_scan(
            session,
            other,
            [f"198.51.100.{index}" for index in range(1, 201)],
        )
        session.commit()

        topology = build_cluster_topology(
            session,
            LiteralResolver(),
            target_cluster_id=selected.id,
        )

        assert {
            (edge["data"]["source"], edge["data"]["target"])
            for edge in topology["edges"]
        } == {
            (f"cluster-{selected.id}", f"cluster-{peer.id}"),
            (f"cluster-{selected.id}", "external-203.0.113.8"),
            (f"cluster-{peer.id}", f"cluster-{selected.id}"),
        }
        assert not any(
            detail["source_device_id"] == other.id
            for edge in topology["edges"]
            for detail in edge["data"]["connections"]
        )


def test_historical_target_cluster_keeps_outbound_and_inbound(app):
    now = datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc)
    with app.state.session_factory() as session:
        selected = Cluster(name="目标集群")
        peer = Cluster(name="对端集群")
        unrelated = Cluster(name="无关集群")
        session.add_all([selected, peer, unrelated])
        session.flush()
        selected_device = add_device(
            session,
            app,
            "selected",
            "10.0.0.1",
            selected,
        )
        selected_peer = add_device(
            session,
            app,
            "selected-peer",
            "10.0.0.2",
            selected,
        )
        inbound_source = add_device(
            session,
            app,
            "inbound",
            "10.0.1.1",
            peer,
        )
        unrelated_source = add_device(
            session,
            app,
            "unrelated",
            "10.0.2.1",
            unrelated,
        )
        add_scan(
            session,
            selected_device,
            [selected_peer.host, "203.0.113.8"],
            started_at=now - timedelta(hours=2),
        )
        add_scan(
            session,
            inbound_source,
            [selected_device.host],
            started_at=now - timedelta(hours=1),
        )
        add_scan(
            session,
            unrelated_source,
            ["198.51.100.9"],
            started_at=now - timedelta(hours=1),
        )
        session.commit()

        topology = build_cluster_topology(
            session,
            LiteralResolver(),
            window="1d",
            now=now,
            target_cluster_id=selected.id,
        )

        pairs = {
            (edge["data"]["source"], edge["data"]["target"])
            for edge in topology["edges"]
        }
        assert (
            f"cluster-{selected.id}",
            "external-203.0.113.8",
        ) in pairs
        assert (
            f"cluster-{peer.id}",
            f"cluster-{selected.id}",
        ) in pairs
        assert not any(f"cluster-{unrelated.id}" in pair for pair in pairs)
        assert not any(
            detail["remote_ip"] == selected_peer.host
            for edge in topology["edges"]
            for detail in edge["data"]["connections"]
        )


def test_cluster_topology_maps_ipv4_mapped_remote_to_managed_device(app):
    with app.state.session_factory() as session:
        source = add_device(session, app, "source", "10.160.79.20")
        target = add_device(session, app, "target", "10.160.79.21")
        add_scan(session, source, ["::ffff:10.160.79.21"])
        session.commit()

        topology = build_cluster_topology(session, LiteralResolver())
        pairs = {
            (edge["data"]["source"], edge["data"]["target"])
            for edge in topology["edges"]
        }

        assert (f"device-{source.id}", f"device-{target.id}") in pairs
        assert "external-10.160.79.21" not in {
            node["data"]["id"] for node in topology["nodes"]
        }


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
            edge["data"]["target"]: edge["data"] for edge in topology["edges"]
        }

        assert topology["window"] == "1d"
        assert edges["external-203.0.113.10"]["is_current"] is False
        assert edges["external-203.0.113.20"]["is_current"] is True
        assert edges["external-203.0.113.20"]["count"] == 1
        assert (
            edges["external-203.0.113.20"]["connections"][0]["observation_count"]
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
