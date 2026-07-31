# IPv4-mapped Address Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Treat IPv4-mapped IPv6 and ordinary IPv4 text as the same address throughout collection, topology grouping, managed-device matching, and scan diffs.

**Architecture:** Add one dependency-free normalization function in the collector base module and reuse it at every address boundary. Normalize new collector output at write time and normalize old `ConnectionRecord` values at topology read time without modifying historical rows.

**Tech Stack:** Python 3.10, standard-library `ipaddress`, SQLAlchemy models, pytest

## Global Constraints

- `::ffff:10.160.79.21` must normalize to `10.160.79.21`.
- Native IPv6 must remain IPv6.
- IPv6 zone identifiers must be removed.
- Invalid non-IP strings must remain unchanged.
- Historical database rows must not be updated or deleted.
- Existing connection details must remain present after node aggregation.
- Do not perform Git operations.

---

### Task 1: Add the shared address normalizer

**Files:**
- Modify: `app/collectors/base.py`
- Create: `tests/test_ip_normalization.py`

**Interfaces:**
- Produces: `normalize_ip_address(address: str | None) -> str | None`
- Produces: `address_family(address: str) -> Literal["ipv4", "ipv6"]`

- [ ] **Step 1: Write failing unit tests**

Add:

```python
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("10.160.79.21", "10.160.79.21"),
        ("::ffff:10.160.79.21", "10.160.79.21"),
        ("2001:0db8::1", "2001:db8::1"),
        ("fe80::1%eth0", "fe80::1"),
        ("host.example", "host.example"),
        (None, None),
    ],
)
def test_normalize_ip_address(source, expected):
    assert normalize_ip_address(source) == expected
```

Assert `address_family("::ffff:10.160.79.21") == "ipv4"` and native IPv6 returns
`"ipv6"`.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_ip_normalization.py -q
```

Expected: import failure because the functions do not exist.

- [ ] **Step 3: Implement the pure functions**

Use:

```python
def normalize_ip_address(address: str | None) -> str | None:
    if address is None:
        return None
    candidate = address.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        return str(parsed.ipv4_mapped)
    return str(parsed)
```

Calculate the family from the normalized value.

- [ ] **Step 4: Run focused tests**

Run the test from Step 2 and expect all cases to pass.

### Task 2: Normalize Linux and Windows collector output

**Files:**
- Modify: `app/collectors/linux.py`
- Modify: `app/collectors/windows.py`
- Modify: `tests/test_collectors.py`

**Interfaces:**
- Consumes: `normalize_ip_address()` and `address_family()`
- Produces: normalized `NormalizedConnection.local_ip` and `remote_ip`

- [ ] **Step 1: Add failing parser tests**

Feed Linux `ss` and Windows JSON rows containing
`::ffff:10.160.79.21`. Assert:

```python
assert row.remote_ip == "10.160.79.21"
assert row.address_family == "ipv4"
```

Also cover a mapped local address.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_collectors.py -q
```

Expected: mapped addresses remain in their original representation.

- [ ] **Step 3: Replace collector-local family helpers**

Normalize parsed endpoint addresses before constructing `NormalizedConnection`.
Remove the duplicate `_address_family()` and `_family()` implementations and use
the shared function.

- [ ] **Step 4: Run collector tests**

Run the test from Step 2 and expect all cases to pass.

### Task 3: Normalize topology grouping, details, resolution, and diffs

**Files:**
- Modify: `app/services/topology.py`
- Modify: `tests/test_cluster_topology.py`
- Create: `tests/test_topology_normalization.py`

**Interfaces:**
- Consumes: `normalize_ip_address()`
- Produces: one device-mode peer node for equivalent mapped/ordinary addresses
- Produces: one cluster-mode target for equivalent mapped/ordinary addresses
- Produces: diff keys that treat equivalent addresses as identical

- [ ] **Step 1: Add failing historical-compatibility tests**

Construct historical `ConnectionRecord` objects containing both address forms.
Assert device topology creates one peer node and one edge with `count == 2`.

Construct previous/current scans differing only by mapped representation and
assert:

```python
assert diff["added"] == []
assert diff["removed"] == []
```

Add a cluster topology test in which a remote mapped address matches a managed
device whose host is ordinary IPv4.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_topology_normalization.py tests\test_cluster_topology.py -q
```

Expected: duplicate nodes or incorrect diff entries.

- [ ] **Step 3: Normalize every topology boundary**

- Use normalized local and remote addresses in `connection_key()`.
- Return normalized addresses from `connection_dict()`.
- Group device topology by normalized `remote_ip`.
- Normalize `HostAddressResolver` direct and DNS results.
- Normalize cluster topology remote addresses before managed-device lookup and
  external-node ID generation.

Do not mutate ORM objects or database rows.

- [ ] **Step 4: Run focused topology tests**

Run the tests from Step 2 and expect all cases to pass.

### Task 4: Complete regression and running-service verification

**Files:**
- Modify: `README.md`
- Verify: all changed files

**Interfaces:**
- Produces: documented address normalization behavior
- Produces: running service with historical snapshots rendered without duplicates

- [ ] **Step 1: Document the normalization**

Add a short note that IPv4-mapped IPv6 addresses are displayed and stored as
ordinary IPv4 for new scans, while historical rows are normalized at read time.

- [ ] **Step 2: Run complete verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
node --check app\static\js\topology.js
```

Expected: all checks pass.

- [ ] **Step 3: Restart and probe the service**

Restart only this project's single Uvicorn application, then verify:

```text
GET /devices
GET /topology
GET /api/topology/clusters
```

all return successful responses.

- [ ] **Step 4: Verify a historical mixed-address fixture**

Use a temporary test database containing both forms and confirm the device and
cluster topology APIs return a single target node without altering the stored
rows.

