import ipaddress
import socket
import threading
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
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
    aggregate_current_connections,
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


@dataclass(frozen=True)
class ClusterGraphContext:
    devices: tuple[Device, ...]
    clusters: tuple[Cluster, ...]
    devices_by_id: dict[int, Device]
    cluster_members: dict[int, list[Device]]
    address_owners: dict[str, list[Device]]
    networks_by_cluster: dict[int, tuple[ipaddress.IPv4Network, ...]]
    warnings: list[str]


def load_cluster_graph_context(
    session: Session,
    resolver: HostAddressResolver,
) -> ClusterGraphContext:
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

    return ClusterGraphContext(
        devices=tuple(devices),
        clusters=tuple(clusters),
        devices_by_id={device.id: device for device in devices},
        cluster_members=dict(cluster_members),
        address_owners=dict(address_owners),
        networks_by_cluster=networks_by_cluster,
        warnings=warnings,
    )


def filter_service_connections(
    services: Sequence[dict],
    *,
    protocol: str | None = None,
    state: str | None = None,
    process: str | None = None,
) -> list[dict]:
    process_query = process.strip().lower() if process else ""
    if not protocol and not state and not process_query:
        return list(services)
    rows = []
    for service in services:
        if protocol and service["protocol"] != protocol:
            continue
        if state and service["state"] != state:
            continue
        if process_query:
            observed = service.get("observed_pids") or ()
            pids = " ".join(str(pid) for pid in observed)
            if not pids and service.get("pid") is not None:
                pids = str(service["pid"])
            haystack = f"{service.get('process_name') or ''} {pids}".lower()
            if process_query not in haystack:
                continue
        rows.append(service)
    return rows


def _service_edge_target(
    service: dict,
    source_device: Device,
    context: ClusterGraphContext,
) -> tuple[str, Device | None] | None:
    normalized_remote = service["remote_ip"]
    owners = context.address_owners.get(normalized_remote, [])
    target_device = owners[0] if len(owners) == 1 else None
    if target_device is not None:
        if target_device.id == source_device.id:
            return None
        if (
            source_device.cluster_id is not None
            and source_device.cluster_id == target_device.cluster_id
        ):
            return None
        return _managed_node_id(target_device), target_device
    if not owners and _is_cluster_internal_address(
        normalized_remote,
        source_device.cluster_id,
        context.networks_by_cluster,
    ):
        return None
    return f"external-{normalized_remote}", None


def group_cluster_edges(
    services: Sequence[dict],
    context: ClusterGraphContext,
) -> dict[tuple[str, str], list[dict]]:
    edge_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for service in services:
        source_device = context.devices_by_id[service["source_device_id"]]
        source_id = _managed_node_id(source_device)
        mapped = _service_edge_target(service, source_device, context)
        if mapped is None:
            continue
        target_id, target_device = mapped
        if source_id == target_id:
            continue
        edge_groups[(source_id, target_id)].append(
            {
                **service,
                "source_device_name": source_device.name,
                "target_device_id": (
                    target_device.id if target_device else None
                ),
                "target_device_name": (
                    target_device.name if target_device else None
                ),
            }
        )
    return edge_groups


def build_cluster_topology(
    session: Session,
    resolver: HostAddressResolver,
    window: TopologyWindow = "current",
    now: datetime | None = None,
    target_cluster_id: int | None = None,
    embed_connections: bool = True,
    protocol: str | None = None,
    state: str | None = None,
    process: str | None = None,
) -> dict:
    context = load_cluster_graph_context(session, resolver)
    devices = context.devices
    clusters = context.clusters
    if (
        target_cluster_id is not None
        and not any(cluster.id == target_cluster_id for cluster in clusters)
    ):
        raise ValueError("集群不存在")
    devices_by_id = context.devices_by_id
    warnings = list(context.warnings)
    cluster_members = context.cluster_members

    device_ids = [device.id for device in devices]
    latest_scans = load_current_scans(
        session,
        device_ids,
        with_connections=False,
    )
    current_scan_ids = {scan.id for scan in latest_scans.values()}
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
    if window == "current":
        services = aggregate_current_connections(
            session,
            latest_scans,
            source_device_ids=source_device_ids,
            inbound_addresses=inbound_addresses,
        )
    else:
        services = aggregate_historical_connections(
            session,
            device_ids,
            current_scan_ids,
            window,
            now=now,
            source_device_ids=source_device_ids,
            inbound_addresses=inbound_addresses,
        )
    services = filter_service_connections(
        services,
        protocol=protocol,
        state=state,
        process=process,
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

    edge_groups = group_cluster_edges(services, context)
    for _, target_id in edge_groups:
        if not target_id.startswith("external-"):
            continue
        normalized_remote = target_id.removeprefix("external-")
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

    edges = []
    for index, ((source_id, target_id), details) in enumerate(
        sorted(edge_groups.items())
    ):
        current_count = sum(1 for detail in details if detail["is_current"])
        historical_count = len(details) - current_count
        observation_count = sum(
            detail["observation_count"] for detail in details
        )
        edge_data = {
            "id": f"cluster-edge-{index}",
            "source": source_id,
            "target": target_id,
            "label": str(len(details)),
            "count": len(details),
            "current_count": current_count,
            "historical_count": historical_count,
            "observation_count": observation_count,
            "is_current": current_count > 0,
        }
        if embed_connections:
            edge_data["connections"] = details
        edges.append({"data": edge_data})
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


def _node_source_devices(
    node_id: str,
    context: ClusterGraphContext,
) -> list[Device]:
    if node_id.startswith("cluster-"):
        try:
            cluster_id = int(node_id.removeprefix("cluster-"))
        except ValueError:
            return []
        return list(context.cluster_members.get(cluster_id, []))
    if node_id.startswith("device-"):
        try:
            device_id = int(node_id.removeprefix("device-"))
        except ValueError:
            return []
        device = context.devices_by_id.get(device_id)
        return [device] if device is not None else []
    return []


def _node_target_addresses(
    node_id: str,
    context: ClusterGraphContext,
    resolver: HostAddressResolver,
) -> set[str]:
    if node_id.startswith("external-"):
        return {node_id.removeprefix("external-")}
    devices = _node_source_devices(node_id, context)
    addresses = {
        address
        for device in devices
        for address in resolver.resolve(device.host)
    }
    addresses.update(
        f"::ffff:{address}"
        for address in tuple(addresses)
        if ":" not in address
    )
    return addresses


def build_edge_connections(
    session: Session,
    resolver: HostAddressResolver,
    *,
    source_id: str,
    target_id: str,
    window: TopologyWindow,
    now: datetime | None = None,
    protocol: str | None = None,
    state: str | None = None,
    process: str | None = None,
) -> list[dict]:
    """Connections of one graph edge, aggregated on demand.

    The pair narrows the observation query with AND semantics (source
    devices x target addresses), then the shared edge-grouping rules keep
    only services that the graph would place on this exact edge.
    """
    context = load_cluster_graph_context(session, resolver)
    source_devices = _node_source_devices(source_id, context)
    target_addresses = _node_target_addresses(target_id, context, resolver)
    if not source_devices or not target_addresses:
        return []
    source_device_ids = {device.id for device in source_devices}
    latest_scans = load_current_scans(
        session,
        source_device_ids,
        with_connections=False,
    )
    current_scan_ids = {scan.id for scan in latest_scans.values()}
    if window == "current":
        services = aggregate_current_connections(
            session,
            latest_scans,
            remote_addresses=target_addresses,
        )
    else:
        services = aggregate_historical_connections(
            session,
            source_device_ids,
            current_scan_ids,
            window,
            now=now,
            remote_addresses=target_addresses,
        )
    services = filter_service_connections(
        services,
        protocol=protocol,
        state=state,
        process=process,
    )
    edge_groups = group_cluster_edges(services, context)
    return edge_groups.get((source_id, target_id), [])
