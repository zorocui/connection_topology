from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import String, case, cast, desc, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import is_loopback_address, normalize_ip_address
from app.models import ConnectionRecord, ScanRun, ScanStatus

TopologyWindow = Literal["current", "1d", "3d", "7d"]
WINDOW_DELTAS = {
    "1d": timedelta(hours=24),
    "3d": timedelta(hours=72),
    "7d": timedelta(hours=168),
}
ConnectionObservation = tuple[int, int, datetime, ConnectionRecord]


def load_current_scans(
    session: Session,
    device_ids: Collection[int],
    *,
    with_connections: bool = True,
) -> dict[int, ScanRun]:
    ids = sorted(set(device_ids))
    if not ids:
        return {}

    ranked = (
        select(
            ScanRun.id.label("scan_id"),
            ScanRun.device_id.label("device_id"),
            func.row_number()
            .over(
                partition_by=ScanRun.device_id,
                order_by=(desc(ScanRun.started_at), desc(ScanRun.id)),
            )
            .label("position"),
        )
        .where(
            ScanRun.device_id.in_(ids),
            ScanRun.status == ScanStatus.SUCCESS,
        )
        .subquery()
    )
    current_ids = list(
        session.scalars(select(ranked.c.scan_id).where(ranked.c.position == 1))
    )
    if not current_ids:
        return {}

    options = [selectinload(ScanRun.device)]
    if with_connections:
        options.append(selectinload(ScanRun.connections))
    scans = session.scalars(
        select(ScanRun)
        .where(ScanRun.id.in_(current_ids))
        .options(*options)
    ).all()
    return {scan.device_id: scan for scan in scans}


def load_topology_scans(
    session: Session,
    device_ids: Collection[int],
    window: TopologyWindow,
    now: datetime | None = None,
) -> tuple[dict[int, ScanRun], list[ScanRun]]:
    ids = sorted(set(device_ids))
    if not ids:
        return {}, []

    current = load_current_scans(session, ids)
    current_ids = [scan.id for scan in current.values()]
    if not current_ids:
        return {}, []

    conditions = [ScanRun.id.in_(current_ids)]
    if window != "current":
        reference = now or datetime.now(timezone.utc)
        conditions.append(ScanRun.started_at >= reference - WINDOW_DELTAS[window])

    scans = list(
        session.scalars(
            select(ScanRun)
            .where(
                ScanRun.device_id.in_(ids),
                ScanRun.status == ScanStatus.SUCCESS,
                or_(*conditions),
            )
            .options(
                selectinload(ScanRun.device),
                selectinload(ScanRun.connections),
            )
            .order_by(ScanRun.started_at, ScanRun.id)
        )
    )
    return current, scans


def _service_key(device_id: int, row: ConnectionRecord) -> tuple:
    return (
        device_id,
        row.protocol,
        normalize_ip_address(row.remote_ip),
        row.remote_port,
        row.process_name or "",
    )


def _aggregate_connection_observations(
    observations: Iterable[ConnectionObservation],
    current_scan_ids: Collection[int],
) -> list[dict]:
    from app.services.topology import connection_dict

    current_ids = set(current_scan_ids)
    groups: dict[tuple, dict] = {}
    for scan_id, device_id, started_at, row in sorted(
        observations,
        key=lambda item: (item[2], item[0], item[3].id),
    ):
        if row.remote_ip is None or is_loopback_address(row.remote_ip):
            continue
        key = _service_key(device_id, row)
        normalized_remote = key[2]
        if normalized_remote is None:
            continue
        values = connection_dict(row)
        values["remote_ip"] = normalized_remote
        bucket = groups.get(key)
        if bucket is None:
            bucket = {
                **values,
                "source_device_id": device_id,
                "scan_id": scan_id,
                "scan_time": started_at.isoformat(),
                "is_current": False,
                "first_seen": started_at.isoformat(),
                "last_seen": started_at.isoformat(),
                "observation_count": 0,
                "_scan_ids": set(),
                "_local_ips": set(),
                "_local_ports": set(),
                "_pids": set(),
            }
            groups[key] = bucket
        else:
            old_hostname = bucket.get("remote_hostname")
            bucket.update(values)
            if not bucket.get("remote_hostname"):
                bucket["remote_hostname"] = old_hostname
            bucket["scan_id"] = scan_id
            bucket["scan_time"] = started_at.isoformat()
            bucket["last_seen"] = started_at.isoformat()

        bucket["is_current"] = bucket["is_current"] or scan_id in current_ids
        bucket["_local_ips"].add(values["local_ip"])
        bucket["_local_ports"].add(values["local_port"])
        if values["pid"] is not None:
            bucket["_pids"].add(values["pid"])
        bucket["_scan_ids"].add(scan_id)

    result = []
    for key in sorted(groups, key=str):
        bucket = groups[key]
        bucket["observation_count"] = len(bucket.pop("_scan_ids"))
        bucket["observed_local_ips"] = sorted(bucket.pop("_local_ips"))
        bucket["observed_local_ports"] = sorted(bucket.pop("_local_ports"))
        bucket["observed_pids"] = sorted(bucket.pop("_pids"))
        result.append(bucket)
    return result


def aggregate_service_connections(
    scans: Sequence[ScanRun],
    current_scan_ids: Collection[int],
) -> list[dict]:
    observations = (
        (scan.id, scan.device_id, scan.started_at, row)
        for scan in scans
        for row in scan.connections
    )
    return _aggregate_connection_observations(observations, current_scan_ids)


def _candidate_connection_ids(
    scan_conditions,
    *,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
):
    base = (
        select(ConnectionRecord.id)
        .join(ScanRun, ScanRun.id == ConnectionRecord.scan_run_id)
        .where(*scan_conditions, ConnectionRecord.remote_ip.is_not(None))
    )
    if source_device_ids is None and inbound_addresses is None:
        return base
    sources = sorted(set(source_device_ids or ()))
    addresses = sorted(set(inbound_addresses or ()))
    outbound = base.where(ScanRun.device_id.in_(sources))
    inbound = base.where(ConnectionRecord.remote_ip.in_(addresses))
    return outbound.union(inbound)


def aggregate_current_connections(
    session: Session,
    current_scans: dict[int, ScanRun],
    *,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
) -> list[dict]:
    current_ids = {scan.id for scan in current_scans.values()}
    if not current_ids:
        return []
    candidate_ids = _candidate_connection_ids(
        [ScanRun.id.in_(current_ids)],
        source_device_ids=source_device_ids,
        inbound_addresses=inbound_addresses,
    )
    connections = session.scalars(
        select(ConnectionRecord)
        .where(ConnectionRecord.id.in_(candidate_ids))
        .order_by(ConnectionRecord.scan_run_id, ConnectionRecord.id)
    ).all()
    scans_by_id = {scan.id: scan for scan in current_scans.values()}
    observations = (
        (
            connection.scan_run_id,
            scans_by_id[connection.scan_run_id].device_id,
            scans_by_id[connection.scan_run_id].started_at,
            connection,
        )
        for connection in connections
    )
    return _aggregate_connection_observations(observations, current_ids)


def _csv_int_set(value: str | None) -> set[int]:
    return {int(item) for item in value.split(",")} if value else set()


def _csv_str_set(value: str | None) -> set[str]:
    return set(value.split(",")) if value else set()


def aggregate_historical_connections(
    session: Session,
    device_ids: Collection[int],
    current_scan_ids: Collection[int],
    window: TopologyWindow,
    *,
    now: datetime | None = None,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
) -> list[dict]:
    if window == "current":
        raise ValueError("历史聚合不接受 current 时间范围")
    ids = sorted(set(device_ids))
    if not ids:
        return []

    current_ids = set(current_scan_ids)
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - WINDOW_DELTAS[window]
    process_key = func.coalesce(ConnectionRecord.process_name, "").label(
        "process_key"
    )
    conditions = [
        ScanRun.device_id.in_(ids),
        ScanRun.status == ScanStatus.SUCCESS,
        or_(
            ScanRun.started_at >= cutoff,
            ScanRun.id.in_(current_ids),
        ),
        ConnectionRecord.remote_ip.is_not(None),
    ]
    if source_device_ids is not None or inbound_addresses is not None:
        conditions.append(
            or_(
                ScanRun.device_id.in_(sorted(set(source_device_ids or ()))),
                ConnectionRecord.remote_ip.in_(
                    sorted(set(inbound_addresses or ()))
                ),
            )
        )

    eligible = (
        select(
            ConnectionRecord.id.label("connection_id"),
            ConnectionRecord.protocol.label("protocol"),
            ConnectionRecord.local_ip.label("local_ip"),
            ConnectionRecord.local_port.label("local_port"),
            ConnectionRecord.remote_ip.label("remote_ip"),
            ConnectionRecord.remote_port.label("remote_port"),
            ConnectionRecord.state.label("state"),
            ConnectionRecord.pid.label("pid"),
            ConnectionRecord.process_name.label("process_name"),
            ConnectionRecord.remote_hostname.label("remote_hostname"),
            ScanRun.id.label("scan_id"),
            ScanRun.device_id.label("device_id"),
            ScanRun.started_at.label("started_at"),
            process_key,
        )
        .join(ScanRun, ScanRun.id == ConnectionRecord.scan_run_id)
        .where(*conditions)
        .subquery()
    )
    raw_key = (
        eligible.c.device_id,
        eligible.c.protocol,
        eligible.c.remote_ip,
        eligible.c.remote_port,
        eligible.c.process_key,
    )
    payload_separator = "\x1f"
    latest_marker = (
        func.lpad(
            cast(
                func.floor(
                    func.extract("epoch", eligible.c.started_at) * 1000000
                ),
                String,
            ),
            20,
            "0",
        )
        + literal("|")
        + func.lpad(cast(eligible.c.scan_id, String), 20, "0")
        + literal("|")
        + func.lpad(cast(eligible.c.connection_id, String), 20, "0")
    )
    hostname_payload = func.max(
        case(
            (
                eligible.c.remote_hostname.is_not(None),
                latest_marker
                + literal(payload_separator)
                + eligible.c.remote_hostname,
            ),
            else_=None,
        )
    ).label("hostname_payload")
    aggregate_rows = session.execute(
        select(
            *raw_key,
            func.min(eligible.c.started_at).label("first_seen"),
            func.max(eligible.c.started_at).label("last_seen"),
            func.max(latest_marker).label("latest_marker"),
            func.string_agg(
                func.distinct(cast(eligible.c.scan_id, String)), literal(",")
            ).label(
                "scan_ids"
            ),
            func.string_agg(
                func.distinct(eligible.c.local_ip), literal(",")
            ).label(
                "local_ips"
            ),
            func.string_agg(
                func.distinct(cast(eligible.c.local_port, String)), literal(",")
            ).label(
                "local_ports"
            ),
            func.string_agg(
                func.distinct(cast(eligible.c.pid, String)), literal(",")
            ).label("pids"),
            hostname_payload,
        ).group_by(*raw_key)
    ).all()
    if not aggregate_rows:
        return []

    latest_rows = session.execute(
        select(
            ConnectionRecord.id.label("connection_id"),
            ConnectionRecord.protocol.label("protocol"),
            ConnectionRecord.local_ip.label("local_ip"),
            ConnectionRecord.local_port.label("local_port"),
            ConnectionRecord.remote_port.label("remote_port"),
            ConnectionRecord.state.label("state"),
            ConnectionRecord.pid.label("pid"),
            ConnectionRecord.process_name.label("process_name"),
            ScanRun.id.label("scan_id"),
            ScanRun.device_id.label("device_id"),
            ScanRun.started_at.label("started_at"),
        )
        .join(ScanRun, ScanRun.id == ConnectionRecord.scan_run_id)
        .where(
            ConnectionRecord.id.in_(
                int(row.latest_marker.rsplit("|", 1)[1])
                for row in aggregate_rows
            )
        )
    ).all()
    latest_by_id = {row.connection_id: row for row in latest_rows}

    groups: dict[tuple, dict] = {}
    for row in aggregate_rows:
        latest = latest_by_id[int(row.latest_marker.rsplit("|", 1)[1])]
        hostname = (
            row.hostname_payload.split(payload_separator, 1)[1]
            if row.hostname_payload
            else None
        )
        normalized_remote = normalize_ip_address(row.remote_ip)
        if normalized_remote is None or is_loopback_address(normalized_remote):
            continue
        normalized_key = (
            row.device_id,
            row.protocol,
            normalized_remote,
            row.remote_port,
            row.process_key,
        )
        scan_ids = _csv_int_set(row.scan_ids)
        local_ips = {
            normalized
            for value in _csv_str_set(row.local_ips)
            if (normalized := normalize_ip_address(value)) is not None
        }
        local_ports = _csv_int_set(row.local_ports)
        pids = _csv_int_set(row.pids)
        latest_local_ip = normalize_ip_address(latest.local_ip)
        assert latest_local_ip is not None
        latest_marker = (
            latest.started_at,
            latest.scan_id,
            latest.connection_id,
        )
        bucket = groups.get(normalized_key)
        if bucket is None:
            bucket = {
                "id": latest.connection_id,
                "protocol": latest.protocol,
                "address_family": (
                    "ipv6" if ":" in latest_local_ip else "ipv4"
                ),
                "local_ip": latest_local_ip,
                "local_port": latest.local_port,
                "remote_ip": normalized_remote,
                "remote_port": latest.remote_port,
                "state": latest.state,
                "pid": latest.pid,
                "process_name": latest.process_name,
                "remote_hostname": hostname,
                "source_device_id": latest.device_id,
                "scan_id": latest.scan_id,
                "scan_time": latest.started_at.isoformat(),
                "first_seen": row.first_seen.isoformat(),
                "last_seen": row.last_seen.isoformat(),
                "_first_seen": row.first_seen,
                "_last_seen": row.last_seen,
                "_latest_marker": latest_marker,
                "_scan_ids": scan_ids,
                "_local_ips": local_ips,
                "_local_ports": local_ports,
                "_pids": pids,
            }
            groups[normalized_key] = bucket
            continue

        bucket["_scan_ids"].update(scan_ids)
        bucket["_local_ips"].update(local_ips)
        bucket["_local_ports"].update(local_ports)
        bucket["_pids"].update(pids)
        if row.first_seen < bucket["_first_seen"]:
            bucket["_first_seen"] = row.first_seen
            bucket["first_seen"] = row.first_seen.isoformat()
        if row.last_seen > bucket["_last_seen"]:
            bucket["_last_seen"] = row.last_seen
            bucket["last_seen"] = row.last_seen.isoformat()
        if hostname and not bucket.get("remote_hostname"):
            bucket["remote_hostname"] = hostname
        if latest_marker > bucket["_latest_marker"]:
            previous_hostname = bucket.get("remote_hostname")
            bucket.update(
                {
                    "id": latest.connection_id,
                    "protocol": latest.protocol,
                    "address_family": (
                        "ipv6" if ":" in latest_local_ip else "ipv4"
                    ),
                    "local_ip": latest_local_ip,
                    "local_port": latest.local_port,
                    "remote_port": latest.remote_port,
                    "state": latest.state,
                    "pid": latest.pid,
                    "process_name": latest.process_name,
                    "remote_hostname": hostname or previous_hostname,
                    "scan_id": latest.scan_id,
                    "scan_time": latest.started_at.isoformat(),
                    "_latest_marker": latest_marker,
                }
            )

    result = []
    for key in sorted(groups, key=str):
        bucket = groups[key]
        all_scan_ids = bucket.pop("_scan_ids")
        bucket["is_current"] = bool(all_scan_ids & current_ids)
        bucket["observation_count"] = len(all_scan_ids)
        bucket["observed_local_ips"] = sorted(bucket.pop("_local_ips"))
        bucket["observed_local_ports"] = sorted(bucket.pop("_local_ports"))
        bucket["observed_pids"] = sorted(bucket.pop("_pids"))
        bucket.pop("_first_seen")
        bucket.pop("_last_seen")
        bucket.pop("_latest_marker")
        result.append(bucket)
    return result
