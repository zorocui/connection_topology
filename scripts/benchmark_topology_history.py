from __future__ import annotations

import argparse
import gc
import json
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import insert

from app.database import (
    create_database_engine,
    create_session_factory,
    init_database,
)
from app.models import (
    ConnectionRecord,
    Device,
    OSType,
    ScanRun,
    ScanStatus,
    ScanTrigger,
)
from app.services.topology_history import (
    aggregate_historical_connections,
    load_current_scans,
)

DEVICE_COUNT = 10
SCANS_PER_DEVICE = 48
INSERT_BATCH_SIZE = 20_000


def _seed_database(database_path: Path, rows: int) -> tuple[Any, datetime]:
    engine = create_database_engine(f"sqlite:///{database_path.as_posix()}")
    init_database(engine)
    reference = datetime.now(timezone.utc)

    device_rows = [
        {
            "name": f"benchmark-{device_id}",
            "host": f"10.10.0.{device_id}",
            "os_type": OSType.LINUX,
            "port": 22,
            "username": "benchmark",
            "encrypted_password": "benchmark",
            "cluster_id": None,
            "scan_interval_minutes": 30,
            "scheduled_enabled": False,
            "created_at": reference,
            "updated_at": reference,
        }
        for device_id in range(1, DEVICE_COUNT + 1)
    ]
    scan_rows = []
    scan_devices = []
    for device_id in range(1, DEVICE_COUNT + 1):
        for scan_offset in range(SCANS_PER_DEVICE):
            started_at = reference - timedelta(
                minutes=30 * (SCANS_PER_DEVICE - scan_offset - 1)
            )
            scan_rows.append(
                {
                    "device_id": device_id,
                    "trigger_type": ScanTrigger.SCHEDULED,
                    "status": ScanStatus.SUCCESS,
                    "started_at": started_at,
                    "finished_at": started_at + timedelta(seconds=5),
                    "connection_count": 0,
                }
            )
            scan_devices.append(device_id)

    with engine.begin() as connection:
        connection.execute(insert(Device), device_rows)
        connection.execute(insert(ScanRun), scan_rows)

        batch = []
        scan_count = len(scan_rows)
        for row_number in range(rows):
            scan_index = row_number % scan_count
            scan_id = scan_index + 1
            device_id = scan_devices[scan_index]
            service_id = (row_number // scan_count) % 2_000
            batch.append(
                {
                    "scan_run_id": scan_id,
                    "protocol": "tcp",
                    "address_family": "ipv4",
                    "local_ip": f"10.10.0.{device_id}",
                    "local_port": 30_000 + (row_number % 20_000),
                    "remote_ip": (
                        f"198.18.{service_id // 250}.{service_id % 250 + 1}"
                    ),
                    "remote_port": 8_000 + service_id % 100,
                    "state": "ESTABLISHED",
                    "pid": 1_000 + row_number % 500,
                    "process_name": f"service-{service_id % 20}",
                    "remote_hostname": (
                        f"peer-{service_id}.internal"
                        if row_number % 17 == 0
                        else None
                    ),
                }
            )
            if len(batch) == INSERT_BATCH_SIZE:
                connection.execute(insert(ConnectionRecord), batch)
                batch.clear()
        if batch:
            connection.execute(insert(ConnectionRecord), batch)
        connection.exec_driver_sql("ANALYZE")

    return engine, reference


def run_benchmark(
    rows: int,
    max_seconds: float,
    database_path: Path | None = None,
) -> dict[str, int | float | bool]:
    if rows < 1:
        raise ValueError("rows must be positive")

    temporary_directory = None
    if database_path is None:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="topology-history-benchmark-"
        )
        database_path = Path(temporary_directory.name) / "benchmark.db"
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)

    engine = None
    try:
        engine, reference = _seed_database(database_path, rows)
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            devices = list(range(1, DEVICE_COUNT + 1))
            current_scans = load_current_scans(
                session,
                devices,
                with_connections=False,
            )
            current_scan_ids = [scan.id for scan in current_scans.values()]

            started_at = time.perf_counter()
            services = aggregate_historical_connections(
                session,
                devices,
                current_scan_ids,
                "1d",
                now=reference,
            )
            elapsed_seconds = time.perf_counter() - started_at
            service_groups = len(services)
            del services
            gc.collect()

            scoped_started_at = time.perf_counter()
            scoped_services = aggregate_historical_connections(
                session,
                devices,
                current_scan_ids,
                "1d",
                now=reference,
                source_device_ids={devices[0]},
                inbound_addresses={f"10.10.0.{devices[0]}"},
            )
            scoped_seconds = time.perf_counter() - scoped_started_at
            scoped_groups = len(scoped_services)

            tracemalloc.start()
            memory_probe_started_at = time.perf_counter()
            memory_probe_services = aggregate_historical_connections(
                session,
                devices,
                current_scan_ids,
                "1d",
                now=reference,
            )
            memory_probe_seconds = time.perf_counter() - memory_probe_started_at
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert len(memory_probe_services) == service_groups

        return {
            "raw_rows": rows,
            "service_groups": service_groups,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "peak_python_mib": round(peak_bytes / 1024 / 1024, 2),
            "memory_probe_seconds": round(memory_probe_seconds, 3),
            "target_seconds": max_seconds,
            "within_target": elapsed_seconds <= max_seconds,
            "scoped_groups": scoped_groups,
            "scoped_seconds": round(scoped_seconds, 3),
            "scoped_within_target": scoped_seconds <= max_seconds,
        }
    finally:
        if engine is not None:
            engine.dispose()
        if temporary_directory is not None:
            temporary_directory.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark exact SQLite historical topology aggregation."
    )
    parser.add_argument("--rows", type=int, default=1_370_000)
    parser.add_argument("--max-seconds", type=float, default=10.0)
    parser.add_argument("--database-path", type=Path)
    arguments = parser.parse_args()

    result = run_benchmark(
        arguments.rows,
        arguments.max_seconds,
        arguments.database_path,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["within_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
