"""service observations preaggregation

Revision ID: 20260807_0002
Revises: 20260805_0001
Create Date: 2026-08-07 14:00:00.000000

Adds the connection_service_observations table (one row per successful scan
and service key) and backfills it from existing connection records in
batches. The backfill is idempotent, so an interrupted migration can be
rerun safely.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.services.service_observations import backfill_service_observations

# revision identifiers, used by Alembic.
revision: str = "20260807_0002"
down_revision: str | Sequence[str] | None = "20260805_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema and backfill observations."""
    op.create_table(
        "connection_service_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scan_run_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("remote_ip", sa.String(length=255), nullable=False),
        sa.Column("remote_port", sa.Integer(), nullable=False),
        sa.Column("process_name", sa.String(length=255), nullable=False),
        sa.Column("sample_connection_id", sa.Integer(), nullable=False),
        sa.Column("remote_hostname", sa.String(length=255), nullable=True),
        sa.Column("local_ips", sa.Text(), nullable=False),
        sa.Column("local_ports", sa.Text(), nullable=False),
        sa.Column("pids", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["scan_run_id"], ["scan_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scan_run_id",
            "protocol",
            "remote_ip",
            "remote_port",
            "process_name",
            name="uq_observation_scan_service",
        ),
    )
    op.create_index(
        "ix_observation_device_started",
        "connection_service_observations",
        ["device_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_observation_remote_started",
        "connection_service_observations",
        ["remote_ip", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_observation_scan",
        "connection_service_observations",
        ["scan_run_id"],
        unique=False,
    )
    backfill_service_observations(op.get_bind())


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_observation_scan", table_name="connection_service_observations"
    )
    op.drop_index(
        "ix_observation_remote_started",
        table_name="connection_service_observations",
    )
    op.drop_index(
        "ix_observation_device_started",
        table_name="connection_service_observations",
    )
    op.drop_table("connection_service_observations")
