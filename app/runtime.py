import os


def resolve_web_workers(
    configured: int | None,
    *,
    cpu_count: int | None = None,
) -> int:
    if configured is not None:
        return configured
    detected = os.cpu_count() if cpu_count is None else cpu_count
    return min(max(detected or 1, 1), 8)
