import ipaddress
from dataclasses import dataclass
from typing import Literal, Protocol


_normalize_cache: dict[str, str | None] = {}
_CACHE_MISS = object()


def _is_canonical_ipv4(candidate: str) -> bool:
    parts = candidate.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if (
            not part.isascii()
            or not part.isdigit()
            or len(part) > 3
            or (len(part) > 1 and part[0] == "0")
            or int(part) > 255
        ):
            return False
    return True


def normalize_ip_address(address: str | None) -> str | None:
    if address is None:
        return None
    cached = _normalize_cache.get(address, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached
    result = _normalize_ip_address_uncached(address)
    _normalize_cache[address] = result
    return result


def _normalize_ip_address_uncached(address: str) -> str:
    candidate = address.split("%", 1)[0]
    if _is_canonical_ipv4(candidate):
        return candidate
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def is_loopback_address(address: str | None) -> bool:
    normalized = normalize_ip_address(address)
    if normalized is None:
        return False
    if _is_canonical_ipv4(normalized):
        return normalized.split(".", 1)[0] == "127"
    if ":" not in normalized:
        return False
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def address_family(address: str) -> Literal["ipv4", "ipv6"]:
    normalized = normalize_ip_address(address)
    assert normalized is not None
    try:
        return "ipv6" if ipaddress.ip_address(normalized).version == 6 else "ipv4"
    except ValueError:
        return "ipv6" if ":" in normalized else "ipv4"


@dataclass(frozen=True, slots=True)
class DeviceConnectionSpec:
    host: str
    port: int
    username: str
    device_id: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedConnection:
    protocol: Literal["tcp", "udp"]
    address_family: Literal["ipv4", "ipv6"]
    local_ip: str
    local_port: int
    remote_ip: str | None
    remote_port: int | None
    state: str | None
    pid: int | None
    process_name: str | None


@dataclass(frozen=True, slots=True)
class CollectionResult:
    connections: tuple[NormalizedConnection, ...]
    warning: str | None = None


class CollectorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Collector(Protocol):
    def collect(self, device: DeviceConnectionSpec, password: str) -> CollectionResult: ...

    def test_connection(self, device: DeviceConnectionSpec, password: str) -> None: ...
