from app.collectors.base import (
    address_family,
    is_loopback_address,
    normalize_ip_address,
)


def test_normalize_ipv4_mapped_ipv6_address():
    assert normalize_ip_address("::ffff:10.160.79.21") == "10.160.79.21"


def test_normalize_regular_addresses_and_zone_identifier():
    assert normalize_ip_address("10.160.79.21") == "10.160.79.21"
    assert normalize_ip_address("2001:db8::1") == "2001:db8::1"
    assert normalize_ip_address("fe80::1%eth0") == "fe80::1"


def test_normalize_none_and_invalid_address():
    assert normalize_ip_address(None) is None
    assert normalize_ip_address("not-an-ip") == "not-an-ip"


def test_address_family_uses_normalized_address():
    assert address_family("::ffff:10.160.79.21") == "ipv4"
    assert address_family("2001:db8::1") == "ipv6"


def test_loopback_address_detection():
    assert is_loopback_address("127.0.0.1")
    assert is_loopback_address("127.23.45.67")
    assert is_loopback_address("::1")
    assert is_loopback_address("::ffff:127.0.0.1")


def test_non_loopback_and_invalid_address_detection():
    assert not is_loopback_address("10.160.79.21")
    assert not is_loopback_address("2001:db8::1")
    assert not is_loopback_address("not-an-ip")
    assert not is_loopback_address(None)
