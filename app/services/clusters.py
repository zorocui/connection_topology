import ipaddress

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Cluster, ClusterInternalNetwork, Device

MAX_INTERNAL_NETWORKS = 100


class ClusterNotFound(ValueError):
    pass


class ClusterConflict(ValueError):
    pass


def normalize_cluster_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("集群名称不能为空")
    if len(normalized) > 100:
        raise ValueError("集群名称不能超过 100 个字符")
    return normalized


def find_cluster_by_name(session: Session, name: str) -> Cluster | None:
    normalized = normalize_cluster_name(name)
    return session.scalar(
        select(Cluster).where(func.lower(Cluster.name) == normalized.lower())
    )


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
        if network.version != 4:
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
    target_cidrs = set(normalized)
    for network in list(cluster.internal_networks):
        if network.cidr not in target_cidrs:
            cluster.internal_networks.remove(network)

    existing_cidrs = {network.cidr for network in cluster.internal_networks}
    cluster.internal_networks.extend(
        ClusterInternalNetwork(cidr=cidr)
        for cidr in normalized
        if cidr not in existing_cidrs
    )
    session.flush()


def create_cluster(
    session: Session,
    name: str,
    description: str | None = None,
    internal_networks: list[str] | None = None,
    *,
    scan_interval_minutes: int = 5,
    scheduled_enabled: bool = True,
) -> Cluster:
    normalized = normalize_cluster_name(name)
    if find_cluster_by_name(session, normalized):
        raise ClusterConflict("同名集群已存在")
    cluster = Cluster(
        name=normalized,
        description=description.strip() if description and description.strip() else None,
        scan_interval_minutes=scan_interval_minutes,
        scheduled_enabled=scheduled_enabled,
    )
    session.add(cluster)
    session.flush()
    replace_internal_networks(session, cluster, internal_networks or [])
    return cluster


def apply_cluster_scan_policy(
    session: Session,
    cluster: Cluster,
) -> list[Device]:
    members = list(
        session.scalars(
            select(Device).where(Device.cluster_id == cluster.id)
        )
    )
    for device in members:
        device.scan_interval_minutes = cluster.scan_interval_minutes
        device.scheduled_enabled = cluster.scheduled_enabled
    return members


def cluster_scan_values(
    cluster: Cluster | None,
    requested_interval: int,
    requested_enabled: bool,
) -> tuple[int, bool]:
    if cluster is None:
        return requested_interval, requested_enabled
    return cluster.scan_interval_minutes, cluster.scheduled_enabled


def resolve_cluster(
    session: Session,
    cluster_id: int | None = None,
    new_cluster_name: str | None = None,
) -> Cluster | None:
    if cluster_id is not None and new_cluster_name:
        raise ValueError("不能同时选择已有集群和新建集群")
    if cluster_id is not None:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise ClusterNotFound("所选集群不存在")
        return cluster
    if new_cluster_name and new_cluster_name.strip():
        existing = find_cluster_by_name(session, new_cluster_name)
        return existing or create_cluster(session, new_cluster_name)
    return None


def delete_cluster(session: Session, cluster: Cluster) -> None:
    session.execute(
        update(Device).where(Device.cluster_id == cluster.id).values(cluster_id=None)
    )
    session.delete(cluster)
