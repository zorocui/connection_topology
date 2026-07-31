from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import (
    ImportBatchStatus,
    ImportStatus,
    ImportTestStatus,
    OSType,
    ScanBatchStatus,
    ScanBatchType,
    ScanStatus,
    ScanTaskStatus,
    ScanTrigger,
)


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    os_type: OSType
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)
    scan_interval_minutes: int = Field(default=5, ge=1, le=10080)
    scheduled_enabled: bool = True
    history_retention_days: int | None = Field(default=None, ge=1, le=3650)
    cluster_id: int | None = Field(default=None, ge=1)
    new_cluster_name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def set_default_port(self) -> "DeviceCreate":
        if self.cluster_id is not None and self.new_cluster_name:
            raise ValueError("不能同时选择已有集群和新建集群")
        if self.port is None:
            self.port = 22 if self.os_type == OSType.LINUX else 5985
        return self


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    os_type: OSType | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, max_length=1024)
    scan_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    scheduled_enabled: bool | None = None
    history_retention_days: int | None = Field(default=None, ge=1, le=3650)
    cluster_id: int | None = Field(default=None, ge=1)
    new_cluster_name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_cluster_choice(self) -> "DeviceUpdate":
        if self.cluster_id is not None and self.new_cluster_name:
            raise ValueError("不能同时选择已有集群和新建集群")
        return self


class DeviceTest(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    os_type: OSType
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def set_default_port(self) -> "DeviceTest":
        if self.port is None:
            self.port = 22 if self.os_type == OSType.LINUX else 5985
        return self


class DeviceRead(BaseModel):
    id: int
    name: str
    host: str
    os_type: OSType
    port: int
    username: str
    scan_interval_minutes: int
    scheduled_enabled: bool
    history_retention_days: int | None
    effective_history_retention_days: int
    history_retention_source: Literal["device", "cluster", "system"]
    cluster_id: int | None
    cluster_name: str | None
    last_scan_status: ScanStatus | None
    last_scan_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ScanRead(BaseModel):
    id: int
    device_id: int
    trigger_type: ScanTrigger
    status: ScanStatus
    started_at: datetime
    finished_at: datetime | None
    connection_count: int
    warning_message: str | None
    error_code: str | None
    error_message: str | None

    model_config = ConfigDict(from_attributes=True)


class ScanTaskRead(BaseModel):
    id: int
    device_id: int
    trigger_type: ScanTrigger
    priority: int
    status: ScanTaskStatus
    scan_run_id: int | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ScanBatchRead(BaseModel):
    id: int
    batch_type: ScanBatchType
    status: ScanBatchStatus
    cluster_id: int | None
    source_import_batch_id: int | None
    total_tasks: int
    pending_tasks: int
    running_tasks: int
    success_tasks: int
    failed_tasks: int
    created_at: datetime
    finished_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ScanBatchFailureItemRead(BaseModel):
    device_id: int
    device_name: str
    host: str
    cluster_name: str | None
    status: ScanTaskStatus
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ScanBatchFailurePageRead(BaseModel):
    batch_id: int
    batch_status: ScanBatchStatus
    total: int
    page: int
    page_size: int
    pages: int
    items: list[ScanBatchFailureItemRead]

    model_config = ConfigDict(from_attributes=True)


class BatchScanCreate(BaseModel):
    scope: Literal["all", "cluster"]
    cluster_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "BatchScanCreate":
        if self.scope == "cluster" and self.cluster_id is None:
            raise ValueError("扫描集群时必须选择集群")
        if self.scope == "all" and self.cluster_id is not None:
            raise ValueError("扫描全部设备时不能指定集群")
        return self


class SettingsUpdate(BaseModel):
    history_retention_days: int = Field(ge=1, le=3650)


class SettingsRead(SettingsUpdate):
    pass


class ClusterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    internal_networks: list[str] = Field(default_factory=list)
    history_retention_days: int | None = Field(default=None, ge=1, le=3650)
    scan_interval_minutes: int = Field(default=5, ge=1, le=10080)
    scheduled_enabled: bool = True


class ClusterUpdate(ClusterCreate):
    pass


class ClusterRead(BaseModel):
    id: int
    name: str
    description: str | None
    internal_networks: list[str] = Field(default_factory=list)
    history_retention_days: int | None
    effective_history_retention_days: int
    scan_interval_minutes: int
    scheduled_enabled: bool
    device_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ImportRowRead(BaseModel):
    id: int
    row_number: int
    device_name: str | None
    host: str | None
    device_id: int | None
    import_status: ImportStatus
    import_message: str
    test_status: ImportTestStatus
    test_message: str | None

    model_config = ConfigDict(from_attributes=True)


class ImportBatchRead(BaseModel):
    id: int
    filename: str
    status: ImportBatchStatus
    total_rows: int
    imported_rows: int
    skipped_rows: int
    error_rows: int
    test_pending_rows: int
    test_success_rows: int
    test_failed_rows: int
    created_at: datetime
    finished_at: datetime | None
    fatal_error: str | None
    scan_batch_id: int | None

    model_config = ConfigDict(from_attributes=True)
