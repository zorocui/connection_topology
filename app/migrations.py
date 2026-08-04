from sqlalchemy import Engine, inspect, text

LATEST_SCHEMA_VERSION = 8


def run_migrations(engine: Engine) -> None:
    """Apply small, idempotent SQLite-compatible schema upgrades."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_versions "
                "(version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        applied_versions = set(
            connection.execute(text("SELECT version FROM schema_versions")).scalars()
        )
        inspector = inspect(connection)
        if "clusters" in inspector.get_table_names():
            cluster_columns = {
                column["name"] for column in inspector.get_columns("clusters")
            }
            if "history_retention_days" not in cluster_columns:
                connection.execute(
                    text(
                        "ALTER TABLE clusters "
                        "ADD COLUMN history_retention_days INTEGER"
                    )
                )
            if "scan_interval_minutes" not in cluster_columns:
                connection.execute(
                    text(
                        "ALTER TABLE clusters ADD COLUMN "
                        "scan_interval_minutes INTEGER NOT NULL DEFAULT 5"
                    )
                )
            if "scheduled_enabled" not in cluster_columns:
                connection.execute(
                    text(
                        "ALTER TABLE clusters ADD COLUMN "
                        "scheduled_enabled BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
        if "devices" in inspector.get_table_names():
            device_columns = {column["name"] for column in inspector.get_columns("devices")}
            if "cluster_id" not in device_columns:
                connection.execute(
                    text(
                        "ALTER TABLE devices ADD COLUMN cluster_id INTEGER "
                        "REFERENCES clusters(id) ON DELETE SET NULL"
                    )
                )
            if "history_retention_days" not in device_columns:
                connection.execute(
                    text(
                        "ALTER TABLE devices "
                        "ADD COLUMN history_retention_days INTEGER"
                    )
                )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_devices_cluster_id ON devices (cluster_id)")
            )
        if "import_batches" in inspector.get_table_names():
            import_columns = {
                column["name"] for column in inspector.get_columns("import_batches")
            }
            if "scan_batch_id" not in import_columns:
                connection.execute(
                    text(
                        "ALTER TABLE import_batches ADD COLUMN scan_batch_id INTEGER "
                        "REFERENCES scan_batches(id) ON DELETE SET NULL"
                    )
                )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_import_batches_scan_batch_id "
                    "ON import_batches (scan_batch_id)"
                )
            )
        if "scan_tasks" in inspector.get_table_names():
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_tasks_device_active "
                    "ON scan_tasks (device_id) "
                    "WHERE status IN ('PENDING', 'RUNNING')"
                )
            )
        if "scan_runs" in inspector.get_table_names():
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_scan_device_status_started "
                    "ON scan_runs (device_id, status, started_at)"
                )
            )
        if "connection_records" in inspector.get_table_names():
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_connection_history_service "
                    "ON connection_records "
                    "(scan_run_id, remote_ip, remote_port, protocol, process_name)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_connection_remote_scan "
                    "ON connection_records (remote_ip, scan_run_id)"
                )
            )
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS cluster_internal_networks ("
                "id INTEGER PRIMARY KEY, "
                "cluster_id INTEGER NOT NULL "
                "REFERENCES clusters(id) ON DELETE CASCADE, "
                "cidr VARCHAR(18) NOT NULL, "
                "CONSTRAINT uq_cluster_internal_network_cluster_cidr "
                "UNIQUE (cluster_id, cidr)"
                ")"
            )
        )
        if (
            6 not in applied_versions
            and "system_settings" in inspector.get_table_names()
        ):
            connection.execute(
                text(
                    "UPDATE system_settings "
                    "SET history_retention_days = 7 "
                    "WHERE history_retention_days = 30"
                )
            )
        if (
            LATEST_SCHEMA_VERSION not in applied_versions
            and "clusters" in inspector.get_table_names()
            and "devices" in inspector.get_table_names()
        ):
            device_columns = {
                column["name"] for column in inspector.get_columns("devices")
            }
            required_device_columns = {
                "cluster_id",
                "scan_interval_minutes",
                "scheduled_enabled",
            }
            if required_device_columns <= device_columns:
                connection.execute(
                    text(
                        "UPDATE clusters SET "
                        "scan_interval_minutes = COALESCE(("
                        "SELECT devices.scan_interval_minutes FROM devices "
                        "WHERE devices.cluster_id = clusters.id "
                        "GROUP BY devices.scan_interval_minutes "
                        "ORDER BY COUNT(*) DESC, "
                        "devices.scan_interval_minutes ASC LIMIT 1"
                        "), 5), "
                        "scheduled_enabled = COALESCE(("
                        "SELECT MAX(CAST(devices.scheduled_enabled AS INTEGER)) "
                        "FROM devices WHERE devices.cluster_id = clusters.id"
                        "), 1)"
                    )
                )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_cluster_internal_networks_cluster_id "
                "ON cluster_internal_networks (cluster_id)"
            )
        )
        connection.execute(
            text(
                "INSERT OR IGNORE INTO schema_versions (version) VALUES (:version)"
            ),
            {"version": LATEST_SCHEMA_VERSION},
        )
