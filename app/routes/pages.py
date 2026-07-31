from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Cluster, Device, ScanRun, ScanStatus, SystemSetting
from app.services.device_listing import ALLOWED_PAGE_SIZES, list_device_page
from app.timezone import format_beijing

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["beijing_time"] = format_beijing


def _base_context(request: Request, active: str) -> dict:
    return {"request": request, "active": active}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    successful_scans = db.scalars(
        select(ScanRun)
        .where(ScanRun.status == ScanStatus.SUCCESS)
        .order_by(desc(ScanRun.started_at))
    ).all()
    latest_by_device: dict[int, ScanRun] = {}
    for scan in successful_scans:
        latest_by_device.setdefault(scan.device_id, scan)
    active_connections = sum(scan.connection_count for scan in latest_by_device.values())
    failed_count = db.scalar(
        select(func.count()).select_from(ScanRun).where(ScanRun.status == ScanStatus.FAILED)
    )
    context = _base_context(request, "dashboard")
    context.update(
        devices=devices,
        device_count=len(devices),
        healthy_count=sum(d.last_scan_status == ScanStatus.SUCCESS for d in devices),
        active_connections=active_connections,
        failed_count=failed_count or 0,
        recent_scans=db.scalars(select(ScanRun).order_by(desc(ScanRun.started_at)).limit(8)).all(),
    )
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/topology", response_class=HTMLResponse)
def topology_page(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, "topology")
    context["devices"] = db.scalars(select(Device).order_by(Device.name)).all()
    context["clusters"] = db.scalars(select(Cluster).order_by(Cluster.name)).all()
    return templates.TemplateResponse(request, "topology.html", context)


@router.get("/devices", response_class=HTMLResponse)
def devices_page(
    request: Request,
    q: str = Query(default="", max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
):
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=422,
            detail="每页数量仅支持 20、50、100",
        )
    device_page = list_device_page(db, q, page, page_size)
    if page != device_page.page or q != device_page.query:
        params = {
            "q": device_page.query,
            "page": device_page.page,
            "page_size": device_page.page_size,
        }
        if not device_page.query:
            params.pop("q")
        return RedirectResponse(
            url=f"/devices?{urlencode(params)}",
            status_code=303,
        )
    context = _base_context(request, "devices")
    context["devices"] = device_page.items
    context["device_page"] = device_page
    context["clusters"] = db.scalars(
        select(Cluster).order_by(Cluster.name)
    ).all()
    setting = db.get(SystemSetting, 1)
    context["system_retention_days"] = (
        setting.history_retention_days if setting else 7
    )
    return templates.TemplateResponse(request, "devices.html", context)


@router.get("/clusters", response_class=HTMLResponse)
def clusters_page(request: Request):
    context = _base_context(request, "clusters")
    return templates.TemplateResponse(request, "clusters.html", context)


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, "history")
    context["devices"] = db.scalars(select(Device).order_by(Device.name)).all()
    context["scans"] = db.scalars(
        select(ScanRun).order_by(desc(ScanRun.started_at)).limit(100)
    ).all()
    return templates.TemplateResponse(request, "history.html", context)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, "settings")
    setting = db.get(SystemSetting, 1)
    context["retention_days"] = setting.history_retention_days if setting else 7
    return templates.TemplateResponse(request, "settings.html", context)
