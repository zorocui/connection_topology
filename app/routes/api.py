import logging
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import desc, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.collectors import CollectorError
from app.database import get_db
from app.models import (
    Cluster,
    Device,
    ImportBatch,
    ImportRowResult,
    ImportStatus,
    ScanBatch,
    ScanBatchType,
    ScanRun,
    ScanStatus,
    ScanTask,
    ScanTrigger,
    SystemSetting,
)
from app.schemas import (
    BatchScanCreate,
    ClusterCreate,
    ClusterRead,
    ClusterUpdate,
    DeviceCreate,
    DeviceRead,
    DeviceTest,
    DeviceUpdate,
    ImportBatchRead,
    ImportRowRead,
    ScanBatchFailurePageRead,
    ScanBatchRead,
    ScanRead,
    ScanTaskRead,
    SettingsRead,
    SettingsUpdate,
)
from app.services.clusters import (
    ClusterConflict,
    apply_cluster_scan_policy,
    cluster_scan_values,
    create_cluster,
    delete_cluster,
    find_cluster_by_name,
    normalize_cluster_name,
    replace_internal_networks,
    resolve_cluster,
)
from app.services.database_transactions import DatabaseUnavailable, TransactionConflict
from app.services.imports import (
    MAX_IMPORT_BYTES,
    ImportValidationError,
    build_import_report,
    build_import_template,
    import_devices,
)
from app.services.postgres_notifications import notify_topology_changed
from app.services.retention import resolve_device_retention
from app.services.scan_batch_failures import (
    failed_device_ids,
    list_batch_failures,
)
from app.services.scan_queue import (
    COLLECTION_DISABLED_MESSAGE,
    PRIORITY_MANUAL,
    DeviceCollectionDisabled,
    ScanQueueFull,
)
from app.services.scans import ScanService
from app.services.topology import (
    build_cluster_topology,
    build_edge_connections,
    build_topology,
    diff_scans,
)
from app.services.topology_history import (
    TopologyWindow,
    aggregate_historical_connections,
    load_current_scans,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.get("/health")
def health(request: Request):
    try:
        with request.app.state.session_factory() as session:
            session.execute(text("SELECT 1"))
        if not request.app.state.migration_current:
            raise RuntimeError("migration not current")
    except Exception as exc:  # noqa: BLE001 - never expose database driver details
        logger.warning("health database check failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="数据库不可用") from None
    return {"database": "ok", "migration": "current"}


@contextmanager
def _write_request(request: Request, operation_name: str):
    try:
        with request.app.state.transaction_runner.guard(operation_name):
            yield
    except (DatabaseUnavailable, TransactionConflict) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _coordinated_route(operation_name: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                request = next(arg for arg in args if isinstance(arg, Request))
            with _write_request(request, operation_name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def _clear_topology_cache(request: Request) -> None:
    request.app.state.topology_cache.clear()


def _system_retention_days(db: Session) -> int:
    setting = db.get(SystemSetting, 1)
    return setting.history_retention_days if setting else 7


def _cluster_read(
    cluster: Cluster,
    device_count: int,
    system_days: int,
) -> ClusterRead:
    return ClusterRead(
        id=cluster.id,
        name=cluster.name,
        description=cluster.description,
        internal_networks=[
            network.cidr for network in cluster.internal_networks
        ],
        history_retention_days=cluster.history_retention_days,
        effective_history_retention_days=(
            cluster.history_retention_days
            if cluster.history_retention_days is not None
            else system_days
        ),
        scan_interval_minutes=cluster.scan_interval_minutes,
        scheduled_enabled=cluster.scheduled_enabled,
        device_count=device_count,
        created_at=cluster.created_at,
        updated_at=cluster.updated_at,
    )


def _device_read(device: Device, system_days: int) -> DeviceRead:
    policy = resolve_device_retention(device, system_days)
    return DeviceRead(
        id=device.id,
        name=device.name,
        host=device.host,
        os_type=device.os_type,
        port=device.port,
        username=device.username,
        scan_interval_minutes=device.scan_interval_minutes,
        scheduled_enabled=device.scheduled_enabled,
        collection_enabled=device.collection_enabled,
        history_retention_days=device.history_retention_days,
        effective_history_retention_days=policy.days,
        history_retention_source=policy.source,
        cluster_id=device.cluster_id,
        cluster_name=device.cluster_name,
        last_scan_status=device.last_scan_status,
        last_scan_at=device.last_scan_at,
        created_at=device.created_at,
        updated_at=device.updated_at,
    )


def _xlsx_response(content: bytes, filename: str) -> Response:
    encoded = quote(filename)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@router.get("/imports/template")
def download_import_template():
    return _xlsx_response(build_import_template(), "设备批量导入模板.xlsx")


@router.post("/imports", response_model=ImportBatchRead, status_code=status.HTTP_201_CREATED)
async def upload_import(
    file: UploadFile,
    request: Request,
    db: Session = Depends(get_db),
):
    content = await file.read(MAX_IMPORT_BYTES + 1)
    try:
        batch = import_devices(
            db,
            request.app.state.cipher,
            file.filename or "import.xlsx",
            content,
            request.app.state.transaction_runner,
        )
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (DatabaseUnavailable, TransactionConflict) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    imported_rows = db.scalars(
        select(ImportRowResult).where(
            ImportRowResult.batch_id == batch.id,
            ImportRowResult.device_id.is_not(None),
            ImportRowResult.import_status == ImportStatus.IMPORTED,
        )
    ).all()
    if request.app.state.scheduler:
        for row in imported_rows:
            device = db.get(Device, row.device_id)
            if device is not None:
                request.app.state.scheduler.sync_device(device)
    _clear_topology_cache(request)
    request.app.state.import_test_service.schedule_batch(batch.id)
    return batch


@router.get("/imports/{batch_id}", response_model=ImportBatchRead)
def get_import_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    return batch


@router.get("/imports/{batch_id}/rows", response_model=list[ImportRowRead])
def get_import_rows(batch_id: int, db: Session = Depends(get_db)):
    if db.get(ImportBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    return db.scalars(
        select(ImportRowResult)
        .where(ImportRowResult.batch_id == batch_id)
        .order_by(ImportRowResult.row_number)
    ).all()


@router.get("/imports/{batch_id}/report")
def download_import_report(batch_id: int, db: Session = Depends(get_db)):
    try:
        content = build_import_report(db, batch_id)
    except ImportValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _xlsx_response(content, f"设备导入结果-{batch_id}.xlsx")


@router.get("/clusters", response_model=list[ClusterRead])
def list_clusters(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Cluster, func.count(Device.id))
        .outerjoin(Device, Device.cluster_id == Cluster.id)
        .options(selectinload(Cluster.internal_networks))
        .group_by(Cluster.id)
        .order_by(Cluster.name)
    ).all()
    system_days = _system_retention_days(db)
    return [
        _cluster_read(cluster, count, system_days)
        for cluster, count in rows
    ]


@router.post("/clusters", response_model=ClusterRead, status_code=status.HTTP_201_CREATED)
def add_cluster(
    payload: ClusterCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    with _write_request(request, "api_create_cluster"):
        try:
            cluster = create_cluster(
                db,
                payload.name,
                payload.description,
                payload.internal_networks,
            )
            cluster.history_retention_days = payload.history_retention_days
            cluster.scan_interval_minutes = payload.scan_interval_minutes
            cluster.scheduled_enabled = payload.scheduled_enabled
            notify_topology_changed(db)
            db.commit()
            db.refresh(cluster)
        except ClusterConflict as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    _clear_topology_cache(request)
    return _cluster_read(cluster, 0, _system_retention_days(db))


@router.put("/clusters/{cluster_id}", response_model=ClusterRead)
@_coordinated_route("api_update_cluster")
def edit_cluster(
    cluster_id: int,
    payload: ClusterUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="集群不存在")
    try:
        normalized = normalize_cluster_name(payload.name)
        duplicate = find_cluster_by_name(db, normalized)
        if duplicate and duplicate.id != cluster.id:
            raise ClusterConflict("同名集群已存在")
        cluster.name = normalized
        cluster.description = (
            payload.description.strip()
            if payload.description and payload.description.strip()
            else None
        )
        cluster.history_retention_days = payload.history_retention_days
        cluster.scan_interval_minutes = payload.scan_interval_minutes
        cluster.scheduled_enabled = payload.scheduled_enabled
        replace_internal_networks(
            db,
            cluster,
            payload.internal_networks,
        )
        members = apply_cluster_scan_policy(db, cluster)
        notify_topology_changed(db)
        db.commit()
    except ClusterConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(cluster)
    if request.app.state.scheduler:
        for device in members:
            try:
                request.app.state.scheduler.sync_device(device)
            except Exception:
                logger.exception(
                    "Failed to synchronize scheduler for device %s",
                    device.id,
                )
    _clear_topology_cache(request)
    count = db.scalar(select(func.count(Device.id)).where(Device.cluster_id == cluster.id)) or 0
    return _cluster_read(cluster, count, _system_retention_days(db))


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
@_coordinated_route("api_delete_cluster")
def remove_cluster(
    cluster_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="集群不存在")
    delete_cluster(db, cluster)
    notify_topology_changed(db)
    db.commit()
    _clear_topology_cache(request)


def _scan_service(request: Request, db: Session) -> ScanService:
    return ScanService(
        request.app.state.session_factory,
        request.app.state.cipher,
        request.app.state.linux_collector,
        request.app.state.windows_collector,
    )


@router.get("/devices", response_model=list[DeviceRead])
def list_devices(db: Session = Depends(get_db)):
    devices = db.scalars(
        select(Device)
        .options(selectinload(Device.cluster))
        .order_by(Device.name)
    ).all()
    system_days = _system_retention_days(db)
    return [_device_read(device, system_days) for device in devices]


@router.post("/devices/test")
def test_device(payload: DeviceTest, request: Request, db: Session = Depends(get_db)):
    try:
        _scan_service(request, db).test_connection(
            payload.os_type,
            payload.host,
            payload.port,
            payload.username,
            payload.password,
        )
    except CollectorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {"ok": True, "message": "连接测试成功"}


@router.post("/devices", response_model=DeviceRead, status_code=status.HTTP_201_CREATED)
def create_device(payload: DeviceCreate, request: Request, db: Session = Depends(get_db)):
    try:
        _scan_service(request, db).test_connection(
            payload.os_type,
            payload.host,
            payload.port,
            payload.username,
            payload.password,
        )
    except CollectorError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    with _write_request(request, "api_create_device"):
        try:
            cluster = resolve_cluster(db, payload.cluster_id, payload.new_cluster_name)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        scan_interval, scheduled_enabled = cluster_scan_values(
            cluster,
            payload.scan_interval_minutes,
            payload.scheduled_enabled,
        )
        device = Device(
            name=payload.name,
            host=payload.host,
            os_type=payload.os_type,
            port=payload.port,
            username=payload.username,
            encrypted_password=request.app.state.cipher.encrypt(payload.password),
            scan_interval_minutes=scan_interval,
            scheduled_enabled=scheduled_enabled,
            history_retention_days=payload.history_retention_days,
            cluster_id=cluster.id if cluster else None,
        )
        db.add(device)
        try:
            notify_topology_changed(db)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="相同地址、端口和用户名的设备已存在",
            ) from exc
        db.refresh(device)
    _clear_topology_cache(request)
    if request.app.state.scheduler:
        request.app.state.scheduler.sync_device(device)
    return _device_read(device, _system_retention_days(db))

@router.put("/devices/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: int,
    payload: DeviceUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    values = payload.model_dump(exclude_unset=True)
    password = values.pop("password", None)
    cluster_id_was_set = "cluster_id" in values
    cluster_id = values.pop("cluster_id", None)
    new_cluster_name = values.pop("new_cluster_name", None)
    effective_password = (
        password if password else request.app.state.cipher.decrypt(device.encrypted_password)
    )
    connection_changed = any(
        key in values for key in ("host", "port", "username", "os_type")
    ) or bool(password)
    if connection_changed:
        try:
            _scan_service(request, db).test_connection(
                values.get("os_type", device.os_type),
                values.get("host", device.host),
                values.get("port", device.port),
                values.get("username", device.username),
                effective_password,
            )
        except CollectorError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    with _write_request(request, "api_update_device"):
        for key, value in values.items():
            setattr(device, key, value)
        if cluster_id_was_set or new_cluster_name:
            try:
                cluster = resolve_cluster(db, cluster_id, new_cluster_name)
                device.cluster_id = cluster.id if cluster else None
                if cluster is not None:
                    (
                        device.scan_interval_minutes,
                        device.scheduled_enabled,
                    ) = cluster_scan_values(
                        cluster,
                        device.scan_interval_minutes,
                        device.scheduled_enabled,
                    )
            except ValueError as exc:
                db.rollback()
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if password:
            device.encrypted_password = request.app.state.cipher.encrypt(password)
            device.collection_enabled = True
        try:
            notify_topology_changed(db)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="相同地址、端口和用户名的设备已存在",
            ) from exc
        db.refresh(device)
    _clear_topology_cache(request)
    if request.app.state.scheduler:
        request.app.state.scheduler.sync_device(device)
    return _device_read(device, _system_retention_days(db))

@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
@_coordinated_route("api_delete_device")
def delete_device(device_id: int, request: Request, db: Session = Depends(get_db)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    if request.app.state.scheduler:
        request.app.state.scheduler.remove_device(device_id)
    if not request.app.state.scan_queue.cancel_device(device_id, session=db):
        raise HTTPException(status_code=409, detail="该设备正在扫描，请稍后再删除")
    db.delete(device)
    notify_topology_changed(db)
    db.commit()
    _clear_topology_cache(request)


@router.post(
    "/devices/{device_id}/scan",
    response_model=ScanTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_scan(device_id: int, request: Request, db: Session = Depends(get_db)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not device.collection_enabled:
        raise HTTPException(status_code=409, detail=COLLECTION_DISABLED_MESSAGE)
    try:
        return request.app.state.scan_queue.enqueue_device(
            device_id,
            ScanTrigger.MANUAL,
            PRIORITY_MANUAL,
        )
    except DeviceCollectionDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScanQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.get("/scan-tasks/{task_id}", response_model=ScanTaskRead)
def get_scan_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(ScanTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    return task


@router.post(
    "/scan-batches",
    response_model=ScanBatchRead,
    status_code=status.HTTP_201_CREATED,
)
def create_scan_batch(
    payload: BatchScanCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    cluster_id = None
    statement = select(Device.id).where(Device.collection_enabled.is_(True))
    batch_type = ScanBatchType.ALL
    if payload.scope == "cluster":
        cluster = db.get(Cluster, payload.cluster_id)
        if cluster is None:
            raise HTTPException(status_code=404, detail="集群不存在")
        cluster_id = cluster.id
        batch_type = ScanBatchType.CLUSTER
        statement = statement.where(Device.cluster_id == cluster.id)
    device_ids = db.scalars(statement.order_by(Device.id)).all()
    try:
        return request.app.state.scan_queue.create_batch(
            batch_type,
            device_ids,
            cluster_id=cluster_id,
        )
    except ScanQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.get("/scan-batches", response_model=list[ScanBatchRead])
def list_scan_batches(db: Session = Depends(get_db)):
    return db.scalars(
        select(ScanBatch).order_by(desc(ScanBatch.created_at)).limit(20)
    ).all()


@router.get(
    "/scan-batches/{batch_id}/failures",
    response_model=ScanBatchFailurePageRead,
)
def get_scan_batch_failures(
    batch_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str = Query(default="", max_length=200),
    db: Session = Depends(get_db),
):
    try:
        return list_batch_failures(db, batch_id, page, page_size, q)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/scan-batches/{batch_id}/retry-failures",
    response_model=ScanBatchRead,
    status_code=status.HTTP_201_CREATED,
)
def retry_scan_batch_failures(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    if db.get(ScanBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="扫描批次不存在")
    device_ids = failed_device_ids(db, batch_id)
    if not device_ids:
        raise HTTPException(status_code=409, detail="该批次当前没有失败设备")
    try:
        return request.app.state.scan_queue.create_batch(
            ScanBatchType.RETRY,
            device_ids,
        )
    except ScanQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.get("/scan-batches/{batch_id}", response_model=ScanBatchRead)
def get_scan_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="扫描批次不存在")
    return batch


@router.get("/scans", response_model=list[ScanRead])
def list_scans(
    device_id: int | None = None,
    scan_status: ScanStatus | None = Query(default=None, alias="status"),
    started_after: datetime | None = None,
    db: Session = Depends(get_db),
):
    statement = select(ScanRun).order_by(desc(ScanRun.started_at)).limit(500)
    if device_id is not None:
        statement = statement.where(ScanRun.device_id == device_id)
    if scan_status is not None:
        statement = statement.where(ScanRun.status == scan_status)
    if started_after is not None:
        statement = statement.where(ScanRun.started_at >= started_after)
    return db.scalars(statement).all()


def _scan_with_connections(db: Session, scan_id: int) -> ScanRun:
    scan = db.scalar(
        select(ScanRun)
        .where(ScanRun.id == scan_id)
        .options(selectinload(ScanRun.device), selectinload(ScanRun.connections))
    )
    if scan is None:
        raise HTTPException(status_code=404, detail="采集批次不存在")
    return scan


@router.get("/scans/{scan_id}", response_model=ScanRead)
def get_scan(scan_id: int, db: Session = Depends(get_db)):
    scan = db.get(ScanRun, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="采集批次不存在")
    return scan


@router.get("/scans/{scan_id}/topology")
def get_topology(scan_id: int, db: Session = Depends(get_db)):
    scan = _scan_with_connections(db, scan_id)
    if scan.status != ScanStatus.SUCCESS:
        raise HTTPException(status_code=409, detail="失败或运行中的批次没有完整拓扑")
    return build_topology(scan)


@router.get("/devices/{device_id}/topology")
def get_latest_topology(
    device_id: int,
    request: Request,
    window: TopologyWindow = Query("current"),
    db: Session = Depends(get_db),
):
    def build() -> dict:
        current_scans = load_current_scans(db, [device_id])
        scan = current_scans.get(device_id)
        if scan is None:
            raise HTTPException(status_code=404, detail="该设备还没有成功采集快照")
        services = None
        if window != "current":
            services = aggregate_historical_connections(
                db,
                [device_id],
                {scan.id},
                window,
            )
        return build_topology(scan, window=window, services=services)

    if window == "current":
        return build()
    cache_key = ("device", device_id, window)
    return request.app.state.topology_cache.get_or_compute(cache_key, build)


@router.get("/topology/clusters")
def get_cluster_topology(
    request: Request,
    window: TopologyWindow = Query("current"),
    cluster_id: int | None = Query(default=None, ge=1),
    protocol: str | None = Query(default=None),
    state: str | None = Query(default=None),
    process: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if cluster_id is not None and db.get(Cluster, cluster_id) is None:
        raise HTTPException(status_code=404, detail="集群不存在")

    def build() -> dict:
        return build_cluster_topology(
            db,
            request.app.state.address_resolver,
            window=window,
            target_cluster_id=cluster_id,
            embed_connections=False,
            protocol=protocol,
            state=state,
            process=process,
        )

    if window == "current":
        return build()
    cache_key = (
        "cluster",
        cluster_id,
        window,
        protocol or "",
        state or "",
        process or "",
    )
    return request.app.state.topology_cache.get_or_compute(cache_key, build)


@router.get("/topology/edge-connections")
def get_edge_connections(
    request: Request,
    source: str = Query(min_length=1),
    target: str = Query(min_length=1),
    window: TopologyWindow = Query("current"),
    protocol: str | None = Query(default=None),
    state: str | None = Query(default=None),
    process: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    def build() -> dict:
        rows = build_edge_connections(
            db,
            request.app.state.address_resolver,
            source_id=source,
            target_id=target,
            window=window,
            protocol=protocol,
            state=state,
            process=process,
        )
        return {
            "source": source,
            "target": target,
            "total": len(rows),
            "connections": rows,
        }

    if window == "current":
        result = build()
    else:
        cache_key = (
            "edge-connections",
            source,
            target,
            window,
            protocol or "",
            state or "",
            process or "",
        )
        result = request.app.state.topology_cache.get_or_compute(cache_key, build)
    return {
        "source": result["source"],
        "target": result["target"],
        "total": result["total"],
        "connections": result["connections"][offset : offset + limit],
    }


@router.get("/scans/{scan_id}/diff")
def get_scan_diff(scan_id: int, db: Session = Depends(get_db)):
    current = _scan_with_connections(db, scan_id)
    previous_id = db.scalar(
        select(ScanRun.id)
        .where(
            ScanRun.device_id == current.device_id,
            ScanRun.status == ScanStatus.SUCCESS,
            ScanRun.started_at < current.started_at,
        )
        .order_by(desc(ScanRun.started_at))
    )
    if previous_id is None:
        return {
            "previous_scan_id": None,
            "current_scan_id": current.id,
            "added": [connection for edge in build_topology(current)["edges"] for connection in edge["data"]["connections"]],
            "removed": [],
        }
    return diff_scans(_scan_with_connections(db, previous_id), current)


@router.get("/settings", response_model=SettingsRead)
def get_settings_row(db: Session = Depends(get_db)):
    setting = db.get(SystemSetting, 1)
    return SettingsRead(history_retention_days=setting.history_retention_days if setting else 7)


@router.put("/settings", response_model=SettingsRead)
@_coordinated_route("api_update_settings")
def update_settings(
    payload: SettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    setting = db.get(SystemSetting, 1)
    if setting is None:
        setting = SystemSetting(id=1, history_retention_days=payload.history_retention_days)
        db.add(setting)
    else:
        setting.history_retention_days = payload.history_retention_days
    db.commit()
    return SettingsRead(history_retention_days=setting.history_retention_days)
