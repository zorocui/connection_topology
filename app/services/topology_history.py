from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Literal

from sqlalchemy import (
    Integer,
    String,
    bindparam,
    case,
    cast,
    desc,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import is_loopback_address, normalize_ip_address
from app.models import (
    ConnectionRecord,
    ConnectionServiceObservation,
    ScanRun,
    ScanStatus,
)

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


def _csv_int_set(value: str | None) -> set[int]:
    return {int(item) for item in value.split(",")} if value else set()


def _csv_str_set(value: str | None) -> set[str]:
    return set(value.split(",")) if value else set()


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _degraded_sample(
    row,
    marker_parts: list[str],
    local_ips: set[str],
    local_ports: set[int],
    pids: set[int],
) -> SimpleNamespace | None:
    """Stand-in for a sample connection row that no longer exists.

    Raw records are purged ahead of observations (raw retention), and a
    history purge can also delete a sample row between the aggregate and
    sample queries. Rebuilding the sample from observation data keeps the
    service group visible in history windows; single-value fields fall back
    to the aggregated CSVs and ``state`` degrades to None. The marker packs
    epoch-micros|scan_run_id|sample_connection_id (see ``latest_marker``).
    """
    if not local_ips:
        return None
    epoch_micros, scan_id, sample_id = (int(part) for part in marker_parts)
    return SimpleNamespace(
        connection_id=sample_id,
        protocol=row.protocol,
        local_ip=min(local_ips),
        local_port=min(local_ports, default=None),
        remote_port=row.remote_port or None,
        state=None,
        pid=min(pids, default=None),
        process_name=row.process_key or None,
        scan_id=scan_id,
        started_at=_EPOCH + timedelta(microseconds=epoch_micros),
        device_id=row.device_id,
    )


def _aggregate_observation_groups(
    session: Session,
    device_ids: Collection[int],
    current_scan_ids: Collection[int],
    *,
    cutoff: datetime | None,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
    remote_addresses: Collection[str] | None = None,
) -> list[dict]:
    """Aggregate per-scan service observations into service groups.

    Reads connection_service_observations (one row per successful scan and
    service key) instead of raw connection records, so the input stays
    proportional to distinct services rather than to raw row counts. When
    cutoff is None only observations from the current scans are aggregated.
    """
    ids = sorted(set(device_ids))
    if not ids:
        return []

    current_ids = set(current_scan_ids)
    observation = ConnectionServiceObservation
    conditions = [observation.device_id.in_(ids)]
    if cutoff is None:
        conditions.append(observation.scan_run_id.in_(current_ids))
    else:
        conditions.append(
            or_(
                observation.started_at >= cutoff,
                observation.scan_run_id.in_(current_ids),
            )
        )
    if source_device_ids is not None or inbound_addresses is not None:
        conditions.append(
            or_(
                observation.device_id.in_(sorted(set(source_device_ids or ()))),
                observation.remote_ip.in_(
                    sorted(set(inbound_addresses or ()))
                ),
            )
        )
    if remote_addresses is not None:
        conditions.append(
            observation.remote_ip.in_(sorted(set(remote_addresses)))
        )

    raw_key = (
        observation.device_id,
        observation.protocol,
        observation.remote_ip,
        observation.remote_port,
        observation.process_name.label("process_key"),
    )
    payload_separator = "\x1f"
    latest_marker = (
        func.lpad(
            cast(
                func.floor(
                    func.extract("epoch", observation.started_at) * 1000000
                ),
                String,
            ),
            20,
            "0",
        )
        + literal("|")
        + func.lpad(cast(observation.scan_run_id, String), 20, "0")
        + literal("|")
        + func.lpad(cast(observation.sample_connection_id, String), 20, "0")
    )
    # The marker is built once per observation row in this inner query; the
    # aggregate below then references the prebuilt column.
    scanned = (
        select(
            *raw_key,
            observation.started_at,
            observation.scan_run_id,
            observation.remote_hostname,
            latest_marker.label("marker"),
        )
        .where(*conditions)
        .subquery()
    )
    group_columns = (
        scanned.c.device_id,
        scanned.c.protocol,
        scanned.c.remote_ip,
        scanned.c.remote_port,
        scanned.c.process_key,
    )
    aggregate_rows = session.execute(
        select(
            *group_columns,
            func.min(scanned.c.started_at).label("first_seen"),
            func.max(scanned.c.started_at).label("last_seen"),
            func.max(scanned.c.marker).label("latest_marker"),
            func.string_agg(
                cast(scanned.c.scan_run_id, String), literal(",")
            ).label("scan_ids"),
            func.max(
                case(
                    (
                        scanned.c.remote_hostname.is_not(None),
                        scanned.c.marker
                        + literal(payload_separator)
                        + scanned.c.remote_hostname,
                    ),
                    else_=None,
                )
            ).label("hostname_payload"),
        ).group_by(*group_columns)
    ).all()
    if not aggregate_rows:
        return []

    # Local ip/port/pid unions are aggregated from the distinct per-scan CSV
    # strings rather than from all observation rows, keeping per-group
    # aggregation state small enough to stay in memory at scale.
    distinct_csvs = (
        select(
            *raw_key,
            observation.local_ips,
            observation.local_ports,
            observation.pids,
        )
        .distinct()
        .where(*conditions)
        .subquery()
    )
    csv_columns = (
        distinct_csvs.c.device_id,
        distinct_csvs.c.protocol,
        distinct_csvs.c.remote_ip,
        distinct_csvs.c.remote_port,
        distinct_csvs.c.process_key,
    )
    csv_rows = session.execute(
        select(
            *csv_columns,
            func.string_agg(
                func.distinct(distinct_csvs.c.local_ips), literal(",")
            ).label("local_ips"),
            func.string_agg(
                func.distinct(distinct_csvs.c.local_ports), literal(",")
            ).label("local_ports"),
            func.string_agg(
                func.distinct(distinct_csvs.c.pids), literal(",")
            ).label("pids"),
        ).group_by(*csv_columns)
    ).all()
    csv_by_key = {
        (
            row.device_id,
            row.protocol,
            row.remote_ip,
            row.remote_port,
            row.process_key,
        ): row
        for row in csv_rows
    }

    # ANY(array) keeps the sample lookup to a single round trip regardless of
    # group count; an expanding IN list exceeds driver parameter limits.
    sample_ids = {
        int(row.latest_marker.rsplit("|", 1)[1]) for row in aggregate_rows
    }
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
            ConnectionRecord.id
            == func.any(bindparam("sample_ids", type_=ARRAY(Integer)))
        ),
        {"sample_ids": sorted(sample_ids)},
    ).all()
    latest_by_id = {row.connection_id: row for row in latest_rows}

    groups: dict[tuple, dict] = {}
    for row in aggregate_rows:
        csv_row = csv_by_key.get(
            (
                row.device_id,
                row.protocol,
                row.remote_ip,
                row.remote_port,
                row.process_key,
            )
        )
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
            for value in _csv_str_set(csv_row.local_ips if csv_row else None)
            if (normalized := normalize_ip_address(value)) is not None
        }
        local_ports = _csv_int_set(csv_row.local_ports if csv_row else None)
        pids = _csv_int_set(csv_row.pids if csv_row else None)
        marker_parts = row.latest_marker.rsplit("|", 2)
        latest = latest_by_id.get(int(marker_parts[2]))
        if latest is None:
            latest = _degraded_sample(
                row, marker_parts, local_ips, local_ports, pids
            )
            if latest is None:
                continue
        latest_local_ip = normalize_ip_address(latest.local_ip)
        assert latest_local_ip is not None
        latest_marker_value = (
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
                "_latest_marker": latest_marker_value,
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
        if latest_marker_value > bucket["_latest_marker"]:
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
                    "_latest_marker": latest_marker_value,
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


def aggregate_current_connections(
    session: Session,
    current_scans: dict[int, ScanRun],
    *,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
    remote_addresses: Collection[str] | None = None,
) -> list[dict]:
    current_ids = {scan.id for scan in current_scans.values()}
    if not current_ids:
        return []
    device_ids = {scan.device_id for scan in current_scans.values()}
    return _aggregate_observation_groups(
        session,
        device_ids,
        current_ids,
        cutoff=None,
        source_device_ids=source_device_ids,
        inbound_addresses=inbound_addresses,
        remote_addresses=remote_addresses,
    )


def aggregate_historical_connections(
    session: Session,
    device_ids: Collection[int],
    current_scan_ids: Collection[int],
    window: TopologyWindow,
    *,
    now: datetime | None = None,
    source_device_ids: Collection[int] | None = None,
    inbound_addresses: Collection[str] | None = None,
    remote_addresses: Collection[str] | None = None,
) -> list[dict]:
    if window == "current":
        raise ValueError("历史聚合不接受 current 时间范围")
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - WINDOW_DELTAS[window]
    return _aggregate_observation_groups(
        session,
        device_ids,
        current_scan_ids,
        cutoff=cutoff,
        source_device_ids=source_device_ids,
        inbound_addresses=inbound_addresses,
        remote_addresses=remote_addresses,
    )
