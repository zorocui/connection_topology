from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.models import Device

RetentionSource = Literal["device", "cluster", "system"]


@dataclass(frozen=True)
class RetentionPolicy:
    days: int
    source: RetentionSource


def resolve_device_retention(
    device: Device,
    system_days: int,
) -> RetentionPolicy:
    if device.history_retention_days is not None:
        return RetentionPolicy(device.history_retention_days, "device")
    if (
        device.cluster is not None
        and device.cluster.history_retention_days is not None
    ):
        return RetentionPolicy(
            device.cluster.history_retention_days,
            "cluster",
        )
    return RetentionPolicy(system_days, "system")
