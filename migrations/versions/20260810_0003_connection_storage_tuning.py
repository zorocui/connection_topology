"""connection table storage tuning

Revision ID: 20260810_0003
Revises: 20260807_0002
Create Date: 2026-08-10 10:00:00.000000

Two storage-level changes for the append-mostly connection tables:

* Lower autovacuum scale factors so the daily retention purge (millions of
  dead rows at a time) is reclaimed incrementally instead of after ~20% of
  the table has died, which the defaults would never reach in time.
* Drop connection_records indexes no query uses. History reads were moved
  to connection_service_observations, and the remaining lookups go through
  the primary key or the scan_run_id-prefixed ix_connection_history_service,
  which also covers the former ix_connection_scan_remote prefix.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260810_0003"
down_revision: str | Sequence[str] | None = "20260807_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VACUUMED_TABLES = ("connection_records", "connection_service_observations")

_DROPPED_INDEXES = (
    ("ix_connection_scan_protocol", ["scan_run_id", "protocol"]),
    ("ix_connection_scan_remote", ["scan_run_id", "remote_ip"]),
    ("ix_connection_scan_process", ["scan_run_id", "process_name"]),
    ("ix_connection_remote_scan", ["remote_ip", "scan_run_id"]),
    ("ix_connection_records_state", ["state"]),
)


def upgrade() -> None:
    for table in _VACUUMED_TABLES:
        op.execute(
            f"ALTER TABLE {table} SET ("
            "autovacuum_vacuum_scale_factor = 0.02, "
            "autovacuum_vacuum_threshold = 50000)"
        )
    for name, _columns in _DROPPED_INDEXES:
        op.drop_index(name, table_name="connection_records")


def downgrade() -> None:
    for name, columns in reversed(_DROPPED_INDEXES):
        op.create_index(name, "connection_records", columns, unique=False)
    for table in _VACUUMED_TABLES:
        op.execute(
            f"ALTER TABLE {table} RESET ("
            "autovacuum_vacuum_scale_factor, "
            "autovacuum_vacuum_threshold)"
        )
