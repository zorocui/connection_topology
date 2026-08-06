from __future__ import annotations

import argparse
import gc
import json
import os
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, text

from app.database import Base, create_session_factory
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


def _reset_database(engine: Engine, allowed_database: str) -> None:
    with engine.begin() as connection:
        database_name = connection.scalar(text("SELECT current_database()"))
        if database_name != allowed_database:
            raise RuntimeError(f"refusing to reset unexpected database: {database_name}")
        table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
        quoted_names = ", ".join(f'"{name}"' for name in table_names)
        connection.execute(text(f"TRUNCATE TABLE {quoted_names} RESTART IDENTITY CASCADE"))


def _seed_database(engine: Engine, rows: int) -> datetime:
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

    return reference


def run_benchmark(
    rows: int,
    max_seconds: float,
    engine: Engine,
) -> dict[str, int | float | bool]:
    if rows < 1:
        raise ValueError("rows must be positive")

    reference = _seed_database(engine, rows)
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark exact PostgreSQL historical topology aggregation."
    )
    parser.add_argument("--rows", type=int, default=1_370_000)
    parser.add_argument("--max-seconds", type=float, default=10.0)
    arguments = parser.parse_args()

    database_url = os.environ.get("BENCHMARK_DATABASE_URL")
    if not database_url:
        parser.error("BENCHMARK_DATABASE_URL is required")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        _reset_database(engine, "connection_topology_benchmark")
        result = run_benchmark(arguments.rows, arguments.max_seconds, engine)
    finally:
        engine.dispose()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["within_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
