from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def format_beijing(
    value: datetime | None,
    format_string: str = "%Y-%m-%d %H:%M:%S",
) -> str:
    if value is None:
        return ""
    utc_value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return utc_value.astimezone(BEIJING_TIMEZONE).strftime(format_string)
