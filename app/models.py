from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OSType(str, enum.Enum):
    LINUX = "linux"
    WINDOWS = "windows"


class ScanTrigger(str, enum.Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    IMPORT = "import"
    BATCH = "batch"


class ScanStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class ScanTaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanBatchStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class ScanBatchType(str, enum.Enum):
    ALL = "all"
    CLUSTER = "cluster"
    IMPORT = "import"
    RETRY = "retry"


class ImportBatchStatus(str, enum.Enum):
    IMPORTING = "importing"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportStatus(str, enum.Enum):
    IMPORTED = "imported"
    SKIPPED = "skipped"
    ERROR = "error"


class ImportTestStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    history_retention_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    scheduled_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    devices: Mapped[list[Device]] = relationship(back_populates="cluster")
    internal_networks: Mapped[list[ClusterInternalNetwork]] = relationship(
        back_populates="cluster",
        cascade="all, delete-orphan",
        order_by="ClusterInternalNetwork.cidr",
    )


class ClusterInternalNetwork(Base):
    __tablename__ = "cluster_internal_networks"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id",
            "cidr",
            name="uq_cluster_internal_network_cluster_cidr",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cidr: Mapped[str] = mapped_column(String(18), nullable=False)

    cluster: Mapped[Cluster] = relationship(back_populates="internal_networks")


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("host", "port", "username", name="uq_device_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    host: Mapped[str] = mapped_column(String(255))
    os_type: Mapped[OSType] = mapped_column(Enum(OSType))
    port: Mapped[int] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(255))
    encrypted_password: Mapped[str] = mapped_column(Text)
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    scheduled_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    history_retention_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    last_scan_status: Mapped[ScanStatus | None] = mapped_column(Enum(ScanStatus), nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    scan_runs: Mapped[list[ScanRun]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    scan_tasks: Mapped[list[ScanTask]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    cluster: Mapped[Cluster | None] = relationship(back_populates="devices")

    @property
    def cluster_name(self) -> str | None:
        return self.cluster.name if self.cluster else None


class ScanRun(Base):
    __tablename__ = "scan_runs"
    __table_args__ = (
        Index("ix_scan_device_started", "device_id", "started_at"),
        Index(
            "ix_scan_device_status_started",
            "device_id",
            "status",
            "started_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    trigger_type: Mapped[ScanTrigger] = mapped_column(Enum(ScanTrigger))
    status: Mapped[ScanStatus] = mapped_column(Enum(ScanStatus), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connection_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    device: Mapped[Device] = relationship(back_populates="scan_runs")
    connections: Mapped[list[ConnectionRecord]] = relationship(
        back_populates="scan_run", cascade="all, delete-orphan"
    )


class ScanBatch(Base):
    __tablename__ = "scan_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_type: Mapped[ScanBatchType] = mapped_column(Enum(ScanBatchType), index=True)
    status: Mapped[ScanBatchStatus] = mapped_column(
        Enum(ScanBatchStatus), default=ScanBatchStatus.PENDING, index=True
    )
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    pending_tasks: Mapped[int] = mapped_column(Integer, default=0)
    running_tasks: Mapped[int] = mapped_column(Integer, default=0)
    success_tasks: Mapped[int] = mapped_column(Integer, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[ScanBatchItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ScanTask(Base):
    __tablename__ = "scan_tasks"
    __table_args__ = (
        Index(
            "uq_scan_tasks_device_active",
            "device_id",
            unique=True,
            sqlite_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
        Index("ix_scan_tasks_claim", "status", "priority", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    trigger_type: Mapped[ScanTrigger] = mapped_column(Enum(ScanTrigger))
    priority: Mapped[int] = mapped_column(Integer, default=20, index=True)
    status: Mapped[ScanTaskStatus] = mapped_column(
        Enum(ScanTaskStatus), default=ScanTaskStatus.PENDING, index=True
    )
    scan_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    device: Mapped[Device] = relationship(back_populates="scan_tasks")
    items: Mapped[list[ScanBatchItem]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class ScanBatchItem(Base):
    __tablename__ = "scan_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "device_id", name="uq_scan_batch_device"),
        Index("ix_scan_batch_items_batch_status", "batch_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("scan_batches.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("scan_tasks.id", ondelete="CASCADE"), index=True
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ScanTaskStatus] = mapped_column(
        Enum(ScanTaskStatus), default=ScanTaskStatus.PENDING, index=True
    )

    batch: Mapped[ScanBatch] = relationship(back_populates="items")
    task: Mapped[ScanTask] = relationship(back_populates="items")


class ConnectionRecord(Base):
    __tablename__ = "connection_records"
    __table_args__ = (
        Index("ix_connection_scan_protocol", "scan_run_id", "protocol"),
        Index("ix_connection_scan_remote", "scan_run_id", "remote_ip"),
        Index("ix_connection_scan_process", "scan_run_id", "process_name"),
        Index(
            "ix_connection_history_service",
            "scan_run_id",
            "remote_ip",
            "remote_port",
            "protocol",
            "process_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"))
    protocol: Mapped[str] = mapped_column(String(8))
    address_family: Mapped[str] = mapped_column(String(8))
    local_ip: Mapped[str] = mapped_column(String(255))
    local_port: Mapped[int] = mapped_column(Integer)
    remote_ip: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)

    scan_run: Mapped[ScanRun] = relationship(back_populates="connections")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    history_retention_days: Mapped[int] = mapped_column(Integer, default=7)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[ImportBatchStatus] = mapped_column(
        Enum(ImportBatchStatus), default=ImportBatchStatus.IMPORTING, index=True
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, default=0)
    test_pending_rows: Mapped[int] = mapped_column(Integer, default=0)
    test_success_rows: Mapped[int] = mapped_column(Integer, default=0)
    test_failed_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fatal_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    scan_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("scan_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )

    rows: Mapped[list[ImportRowResult]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ImportRowResult(Base):
    __tablename__ = "import_row_results"
    __table_args__ = (Index("ix_import_row_batch_number", "batch_id", "row_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"))
    row_number: Mapped[int] = mapped_column(Integer)
    device_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    import_status: Mapped[ImportStatus] = mapped_column(Enum(ImportStatus))
    import_message: Mapped[str] = mapped_column(String(500))
    test_status: Mapped[ImportTestStatus] = mapped_column(
        Enum(ImportTestStatus), default=ImportTestStatus.NOT_APPLICABLE
    )
    test_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")
    device: Mapped[Device | None] = relationship()
