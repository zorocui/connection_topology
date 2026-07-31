import ipaddress
import socket
import threading
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import (
    address_family,
    normalize_ip_address,
)
from app.models import Cluster, ConnectionRecord, Device, ScanRun
from app.services.topology_history import (
    TopologyWindow,
    aggregate_historical_connections,
    aggregate_service_connections,
    load_current_scans,
)


def connection_key(row: ConnectionRecord) -> tuple:
    return (
        row.protocol,
        normalize_ip_address(row.local_ip),
        row.local_port,
        normalize_ip_address(row.remote_ip),
        row.remote_port,
        row.pid,
        row.process_name,
    )


def connection_dict(row: ConnectionRecord) -> dict:
    local_ip = normalize_ip_address(row.local_ip)
    remote_ip = normalize_ip_address(row.remote_ip)
    assert local_ip is not None
    return {
        "id": row.id,
        "protocol": row.protocol,
        "address_family": address_family(local_ip),
        "local_ip": local_ip,
        "local_port": row.local_port,
        "remote_ip": remote_ip,
        "remote_port": row.remote_port,
        "state": row.state,
        "pid": row.pid,
        "process_name": row.process_name,
        "remote_hostname": row.remote_hostname,
    }


def build_topology(
    scan_run: ScanRun,
    scans: Sequence[ScanRun] | None = None,
    window: TopologyWindow = "current",
    services: Sequence[dict] | None = None,
) -> dict:
    device = scan_run.device
    server_id = f"device-{device.id}"
    selected_scans = list(scans) if scans is not None else [scan_run]
    selected_services = (
        list(services)
        if services is not None
        else aggregate_service_connections(selected_scans, {scan_run.id})
    )
    groups: dict[str, list[dict]] = defaultdict(list)
    listeners = [
        connection_dict(row) for row in scan_run.connections if row.remote_ip is None
    ]
    for service in selected_services:
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


def diff_scans(previous: ScanRun, current: ScanRun) -> dict:
    previous_by_key = {connection_key(row): row for row in previous.connections}
    current_by_key = {connection_key(row): row for row in current.connections}
    return {
        "previous_scan_id": previous.id,
        "current_scan_id": current.id,
        "added": [
            connection_dict(current_by_key[key])
            for key in sorted(current_by_key.keys() - previous_by_key.keys(), key=str)
        ],
        "removed": [
            connection_dict(previous_by_key[key])
            for key in sorted(previous_by_key.keys() - current_by_key.keys(), key=str)
        ],
    }


class HostAddressResolver:
    def __init__(self, ttl_minutes: int = 10) -> None:
        self.ttl = timedelta(minutes=ttl_minutes)
        self._cache: dict[str, tuple[datetime, set[str]]] = {}
        self._lock = threading.Lock()

    def resolve(self, host: str) -> set[str]:
        normalized_host = normalize_ip_address(host)
        assert normalized_host is not None
        try:
            ipaddress.ip_address(normalized_host)
            return {normalized_host}
        except ValueError:
            pass
        now = datetime.now(timezone.utc)
        with self._lock:
            cached = self._cache.get(host.lower())
            if cached and now - cached[0] < self.ttl:
                return set(cached[1])
        addresses: set[str] = set()
        try:
            for result in socket.getaddrinfo(host, None):
                address = normalize_ip_address(result[4][0])
                assert address is not None
                try:
                    ipaddress.ip_address(address)
                    addresses.add(address)
                except ValueError:
                    continue
        except OSError:
            addresses = set()
        with self._lock:
            self._cache[host.lower()] = (now, addresses)
        return addresses


def _managed_node_id(device: Device) -> str:
    return f"cluster-{device.cluster_id}" if device.cluster_id else f"device-{device.id}"


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


def build_cluster_topology(
    session: Session,
    resolver: HostAddressResolver,
    window: TopologyWindow = "current",
    now: datetime | None = None,
    target_cluster_id: int | None = None,
) -> dict:
    devices = session.scalars(
        select(Device)
        .options(selectinload(Device.cluster))
        .order_by(Device.name)
    ).all()
    clusters = session.scalars(
        select(Cluster)
        .options(selectinload(Cluster.internal_networks))
        .order_by(Cluster.name)
    ).all()
    if (
        target_cluster_id is not None
        and not any(cluster.id == target_cluster_id for cluster in clusters)
    ):
        raise ValueError("集群不存在")
    devices_by_id = {device.id: device for device in devices}

    warnings: list[str] = []
    networks_by_cluster = _cluster_network_map(clusters, warnings)

    address_owners: dict[str, list[Device]] = defaultdict(list)
    for device in devices:
        for address in resolver.resolve(device.host):
            address_owners[address].append(device)
    warnings.extend(
        f"地址 {address} 同时匹配多台设备，已按外部地址处理"
        for address, owners in address_owners.items()
        if len(owners) > 1
    )

    cluster_members: dict[int, list[Device]] = defaultdict(list)
    for device in devices:
        if device.cluster_id:
            cluster_members[device.cluster_id].append(device)

    device_ids = [device.id for device in devices]
    latest_scans = load_current_scans(
        session,
        device_ids,
        with_connections=window == "current",
    )
    current_scan_ids = {scan.id for scan in latest_scans.values()}
    if window == "current":
        services = aggregate_service_connections(
            list(latest_scans.values()),
            current_scan_ids,
        )
    else:
        source_device_ids = None
        inbound_addresses = None
        if target_cluster_id is not None:
            members = cluster_members.get(target_cluster_id, [])
            source_device_ids = {device.id for device in members}
            normalized_addresses = {
                address
                for device in members
                for address in resolver.resolve(device.host)
            }
            inbound_addresses = set(normalized_addresses)
            inbound_addresses.update(
                f"::ffff:{address}"
                for address in normalized_addresses
                if ":" not in address
            )
        services = aggregate_historical_connections(
            session,
            device_ids,
            current_scan_ids,
            window,
            now=now,
            source_device_ids=source_device_ids,
            inbound_addresses=inbound_addresses,
        )

    nodes_by_id: dict[str, dict] = {}
    for cluster in clusters:
        members = cluster_members.get(cluster.id, [])
        nodes_by_id[f"cluster-{cluster.id}"] = {
            "data": {
                "id": f"cluster-{cluster.id}",
                "label": cluster.name,
                "kind": "cluster",
                "subtitle": f"{len(members)} 台设备",
                "members": [
                    {
                        "id": device.id,
                        "name": device.name,
                        "host": device.host,
                        "last_scan_status": (
                            device.last_scan_status.value
                            if device.last_scan_status
                            else None
                        ),
                        "scan_id": (
                            latest_scans[device.id].id
                            if device.id in latest_scans
                            else None
                        ),
                        "scan_time": (
                            latest_scans[device.id].started_at.isoformat()
                            if device.id in latest_scans
                            else None
                        ),
                    }
                    for device in members
                ],
            }
        }
    for device in devices:
        if device.cluster_id is None:
            scan = latest_scans.get(device.id)
            nodes_by_id[f"device-{device.id}"] = {
                "data": {
                    "id": f"device-{device.id}",
                    "label": device.name,
                    "kind": "device",
                    "subtitle": device.host,
                    "members": [],
                    "scan_id": scan.id if scan else None,
                    "scan_time": scan.started_at.isoformat() if scan else None,
                }
            }

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
            if not owners and _is_cluster_internal_address(
                normalized_remote,
                source_device.cluster_id,
                networks_by_cluster,
            ):
                continue
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
                "target_device_id": target_device.id if target_device else None,
                "target_device_name": target_name if target_device else None,
            }
        )

    edges = []
    for index, ((source_id, target_id), details) in enumerate(
        sorted(edge_groups.items())
    ):
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
    nodes = list(nodes_by_id.values())
    if target_cluster_id is not None:
        target_node_id = f"cluster-{target_cluster_id}"
        edges = [
            edge
            for edge in edges
            if edge["data"]["source"] == target_node_id
            or edge["data"]["target"] == target_node_id
        ]
        visible_node_ids = {target_node_id}
        for edge in edges:
            visible_node_ids.add(edge["data"]["source"])
            visible_node_ids.add(edge["data"]["target"])
        nodes = [
            node for node in nodes if node["data"]["id"] in visible_node_ids
        ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "cluster",
        "window": window,
        "nodes": nodes,
        "edges": edges,
        "warnings": warnings,
    }
