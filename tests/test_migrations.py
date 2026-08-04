from sqlalchemy import create_engine, inspect, text

from app.database import init_database


def test_existing_device_table_is_upgraded_without_data_loss(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE devices (id INTEGER PRIMARY KEY, name VARCHAR(100))")
        )
        connection.execute(text("INSERT INTO devices (id, name) VALUES (1, 'legacy')"))
    init_database(engine)
    inspector = inspect(engine)
    assert "clusters" in inspector.get_table_names()
    assert "cluster_internal_networks" in inspector.get_table_names()
    assert "import_batches" in inspector.get_table_names()
    assert "import_row_results" in inspector.get_table_names()
    assert "scan_batches" in inspector.get_table_names()
    assert "scan_tasks" in inspector.get_table_names()
    assert "scan_batch_items" in inspector.get_table_names()
    assert "cluster_id" in {column["name"] for column in inspector.get_columns("devices")}
    assert "history_retention_days" in {
        column["name"] for column in inspector.get_columns("devices")
    }
    assert "history_retention_days" in {
        column["name"] for column in inspector.get_columns("clusters")
    }
    assert {"scan_interval_minutes", "scheduled_enabled"} <= {
        column["name"] for column in inspector.get_columns("clusters")
    }
    assert "scan_batch_id" in {
        column["name"] for column in inspector.get_columns("import_batches")
    }
    assert "uq_scan_tasks_device_active" in {
        index["name"] for index in inspector.get_indexes("scan_tasks")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM devices WHERE id=1")).scalar() == "legacy"


def test_existing_devices_are_collection_enabled_after_v9_upgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'collection-v9.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE devices ("
                "id INTEGER PRIMARY KEY, name VARCHAR(100), "
                "encrypted_password TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO devices (id, name, encrypted_password) "
                "VALUES (1, 'legacy', 'ciphertext')"
            )
        )

    init_database(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("devices")}
    assert "collection_enabled" in columns
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT collection_enabled FROM devices WHERE id = 1")
        ).scalar() == 1
        assert connection.execute(
            text("SELECT MAX(version) FROM schema_versions")
        ).scalar() == 9


def test_cluster_internal_network_table_and_indexes_are_created(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'networks.db'}")

    init_database(engine)
    init_database(engine)

    inspector = inspect(engine)
    assert "cluster_internal_networks" in inspector.get_table_names()
    columns = {
        column["name"]
        for column in inspector.get_columns("cluster_internal_networks")
    }
    assert columns == {"id", "cluster_id", "cidr"}
    indexes = {
        index["name"]
        for index in inspector.get_indexes("cluster_internal_networks")
    }
    assert "ix_cluster_internal_networks_cluster_id" in indexes
    assert "ix_scan_device_status_started" in {
        index["name"] for index in inspector.get_indexes("scan_runs")
    }
    connection_indexes = {
        index["name"]
        for index in inspector.get_indexes("connection_records")
    }
    assert "ix_connection_history_service" in connection_indexes
    assert "ix_connection_remote_scan" in connection_indexes
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT MAX(version) FROM schema_versions")
        ).scalar() == 9


def test_version_six_changes_legacy_default_but_preserves_custom_value(tmp_path):
    default_engine = create_engine(f"sqlite:///{tmp_path / 'default.db'}")
    custom_engine = create_engine(f"sqlite:///{tmp_path / 'custom.db'}")
    for engine, retention_days in ((default_engine, 30), (custom_engine, 45)):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE schema_versions "
                    "(version INTEGER PRIMARY KEY, applied_at DATETIME)"
                )
            )
            connection.execute(
                text("INSERT INTO schema_versions (version) VALUES (5)")
            )
            connection.execute(
                text(
                    "CREATE TABLE system_settings "
                    "(id INTEGER PRIMARY KEY, history_retention_days INTEGER)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO system_settings "
                    "(id, history_retention_days) VALUES (1, :days)"
                ),
                {"days": retention_days},
            )

        init_database(engine)
        with engine.connect() as connection:
            migrated = connection.execute(
                text(
                    "SELECT history_retention_days "
                    "FROM system_settings WHERE id = 1"
                )
            ).scalar()
        assert migrated == (7 if retention_days == 30 else 45)


def test_version_seven_derives_cluster_scan_policy_from_members(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scan-policy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE schema_versions "
                "(version INTEGER PRIMARY KEY, applied_at DATETIME)"
            )
        )
        connection.execute(
            text("INSERT INTO schema_versions (version) VALUES (6)")
        )
        connection.execute(
            text(
                "CREATE TABLE clusters "
                "(id INTEGER PRIMARY KEY, name VARCHAR(100))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE devices ("
                "id INTEGER PRIMARY KEY, cluster_id INTEGER, "
                "scan_interval_minutes INTEGER, scheduled_enabled BOOLEAN)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO clusters (id, name) VALUES "
                "(1, 'mode'), (2, 'tie'), (3, 'empty')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO devices "
                "(id, cluster_id, scan_interval_minutes, scheduled_enabled) "
                "VALUES (1, 1, 5, 0), (2, 1, 10, 0), (3, 1, 10, 0), "
                "(4, 2, 5, 0), (5, 2, 10, 1)"
            )
        )

    init_database(engine)

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, scan_interval_minutes, scheduled_enabled "
                "FROM clusters ORDER BY id"
            )
        ).all()
    assert rows == [(1, 10, 0), (2, 5, 1), (3, 5, 1)]
