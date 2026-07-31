from dataclasses import dataclass
from math import ceil

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Device

ALLOWED_PAGE_SIZES = frozenset({20, 50, 100})


@dataclass(frozen=True)
class DevicePage:
    items: list[Device]
    query: str
    page: int
    page_size: int
    total_items: int
    total_pages: int
    first_item: int
    last_item: int
    page_links: list[int | None]


def build_page_links(page: int, total_pages: int) -> list[int | None]:
    if total_pages <= 0:
        return []
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    visible = {
        1,
        total_pages,
        *range(max(2, page - 2), min(total_pages, page + 2) + 1),
    }
    links: list[int | None] = []
    previous = 0
    for current in sorted(visible):
        if previous and current - previous > 1:
            links.append(None)
        links.append(current)
        previous = current
    return links


def list_device_page(
    session: Session,
    query: str,
    page: int,
    page_size: int,
) -> DevicePage:
    if page < 1:
        raise ValueError("页码必须大于等于 1")
    if page_size not in ALLOWED_PAGE_SIZES:
        raise ValueError("每页数量仅支持 20、50、100")

    normalized_query = query.strip()
    filters = []
    if normalized_query:
        filters.append(
            or_(
                Device.name.contains(normalized_query, autoescape=True),
                Device.host.contains(normalized_query, autoescape=True),
            )
        )

    count_statement = select(func.count()).select_from(Device)
    if filters:
        count_statement = count_statement.where(*filters)
    total_items = session.scalar(count_statement) or 0
    total_pages = ceil(total_items / page_size) if total_items else 0
    resolved_page = min(page, total_pages) if total_pages else 1

    statement = (
        select(Device)
        .options(selectinload(Device.cluster))
        .order_by(Device.name, Device.id)
        .offset((resolved_page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        statement = statement.where(*filters)
    items = list(session.scalars(statement).all())
    first_item = (resolved_page - 1) * page_size + 1 if items else 0
    last_item = first_item + len(items) - 1 if items else 0

    return DevicePage(
        items=items,
        query=normalized_query,
        page=resolved_page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        first_item=first_item,
        last_item=last_item,
        page_links=build_page_links(resolved_page, total_pages),
    )
