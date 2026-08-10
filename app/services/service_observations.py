from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.collectors.base import NormalizedConnection
from app.models import ScanRun, ScanStatus

# Only these connections are persisted: established TCP sessions (Linux `ss`
# reports ESTAB, Windows reports ESTABLISHED), every UDP row (UDP is stateless
# in the collectors), and listener rows without a remote address. Transient
# TCP states (TIME-WAIT, CLOSE-WAIT, ...) carry little information at hourly
# scan granularity but dominate raw row counts.
_KEPT_TCP_STATES = frozenset({"ESTAB", "ESTABLISHED"})


def keep_connection_record(row: NormalizedConnection) -> bool:
    if row.remote_ip is None:
        return True
    if row.protocol == "udp":
        return True
    return (row.state or "").upper() in _KEPT_TCP_STATES


# One row per (scan, service key); shared by the per-scan sync and the
# migration backfill so both paths aggregate identically. device_id and
# started_at are denormalized from scan_runs so history queries never join.
_SYNC_SQL = text(
    """
    INSERT INTO connection_service_observations (
        scan_run_id,
        device_id,
        started_at,
        protocol,
        remote_ip,
        remote_port,
        process_name,
        sample_connection_id,
        remote_hostname,
        local_ips,
        local_ports,
        pids
    )
    SELECT
        scan.id,
        scan.device_id,
        scan.started_at,
        record.protocol,
        record.remote_ip,
        COALESCE(record.remote_port, 0),
        COALESCE(record.process_name, ''),
        MAX(record.id),
        (array_agg(record.remote_hostname ORDER BY record.id DESC)
            FILTER (WHERE record.remote_hostname IS NOT NULL))[1],
        string_agg(DISTINCT record.local_ip, ','),
        string_agg(DISTINCT CAST(record.local_port AS VARCHAR), ','),
        string_agg(DISTINCT CAST(record.pid AS VARCHAR), ',')
    FROM connection_records AS record
    JOIN scan_runs AS scan ON scan.id = record.scan_run_id
    WHERE scan.id >= :lower_scan_id
      AND scan.id < :upper_scan_id
      AND scan.status = 'SUCCESS'
      AND record.remote_ip IS NOT NULL
    GROUP BY
        scan.id,
        scan.device_id,
        scan.started_at,
        record.protocol,
        record.remote_ip,
        COALESCE(record.remote_port, 0),
        COALESCE(record.process_name, '')
    ON CONFLICT ON CONSTRAINT uq_observation_scan_service DO NOTHING
    """
)

_SCAN_ID_BOUNDS_SQL = text(
    "SELECT min(id), max(id) FROM scan_runs WHERE status = 'SUCCESS'"
)


def sync_service_observations(session: Session, scan: ScanRun) -> None:
    if scan.status is not ScanStatus.SUCCESS:
        return
    session.execute(
        _SYNC_SQL,
        {"lower_scan_id": scan.id, "upper_scan_id": scan.id + 1},
    )


def backfill_service_observations(
    connection: Connection,
    *,
    scan_id_batch_size: int = 2000,
) -> None:
    min_scan_id, max_scan_id = connection.execute(_SCAN_ID_BOUNDS_SQL).one()
    if min_scan_id is None:
        return
    lower = min_scan_id
    while lower <= max_scan_id:
        upper = lower + scan_id_batch_size
        connection.execute(
            _SYNC_SQL,
            {"lower_scan_id": lower, "upper_scan_id": upper},
        )
        lower = upper
