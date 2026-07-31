from app.collectors.base import CollectionResult, CollectorError, NormalizedConnection
from app.collectors.linux import LinuxCollector
from app.collectors.windows import WindowsCollector

__all__ = [
    "CollectionResult",
    "CollectorError",
    "LinuxCollector",
    "NormalizedConnection",
    "WindowsCollector",
]

