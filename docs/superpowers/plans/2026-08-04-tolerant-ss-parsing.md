# Tolerant Linux ss Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve valid Linux connection records when individual `ss` rows are malformed, warn about skipped rows, and log each raw malformed row with its device context.

**Architecture:** Split parsing into a detailed internal result containing valid connections and rejected TCP/UDP candidates, while retaining `parse_ss_output()` as the public tuple-returning API. `LinuxCollector.collect()` will log every rejected row, reject an output only when all TCP/UDP candidates are malformed, and merge parsing warnings with the existing permission-fallback warning. An optional device ID on `DeviceConnectionSpec` supplies stable log correlation without affecting transport behavior.

**Tech Stack:** Python 3.11+, dataclasses, standard-library logging, Paramiko, pytest, pytest `caplog`, Ruff

## Global Constraints

- Do not retry the `ss` command automatically.
- Do not modify batch queueing, concurrency, or failed-batch retry behavior.
- Do not return raw malformed rows to the browser or store them in the database.
- Log raw malformed rows only in the server application log, with device ID, `ss` line number, and rejection reason.
- Preserve existing SSH, timeout, permission fallback, and command-unavailable behavior.
- Treat empty output and output containing no TCP/UDP candidates as a successful empty collection.
- Treat output whose TCP/UDP candidates are all malformed as `parse_error`.

---

### Task 1: Carry Device Identity Into Collector Context

**Files:**
- Modify: `app/collectors/base.py:39-43`
- Modify: `app/services/scans.py:88-94`
- Modify: `app/services/import_testing.py:43-49,203-212,240-247`
- Test: `tests/test_services.py`
- Test: `tests/test_import_testing.py`

**Interfaces:**
- Consumes: existing `Device.id` integer primary key.
- Produces: `DeviceConnectionSpec(host: str, port: int, username: str, device_id: int | None = None)`.
- Produces: `ImportTestTarget.device_id: int` populated from the imported device.

- [ ] **Step 1: Write failing scan-context and import-test-context tests**

Add these imports and test helper to `tests/test_services.py`:

```python
from dataclasses import dataclass, field

from app.collectors.base import CollectionResult, DeviceConnectionSpec
from app.services.scans import ScanService


@dataclass
class RecordingCollector:
    seen_devices: list[DeviceConnectionSpec] = field(default_factory=list)

    def collect(self, device, password):
        self.seen_devices.append(device)
        return CollectionResult(())

    def test_connection(self, device, password):
        self.seen_devices.append(device)
```

Add the scan test to `tests/test_services.py`:

```python
def test_scan_service_passes_device_id_to_collector(app):
    collector = RecordingCollector()
    with app.state.session_factory() as session:
        device = Device(
            name="context-server",
            host="10.0.0.18",
            os_type=OSType.LINUX,
            port=22,
            username="ops",
            encrypted_password=app.state.cipher.encrypt("secret"),
        )
        session.add(device)
        session.commit()
        device_id = device.id
        service = ScanService(
            session,
            app.state.cipher,
            linux_collector=collector,
            windows_collector=collector,
        )
        service.run(device_id, ScanTrigger.MANUAL)

    assert collector.seen_devices[0].device_id == device_id
```

In `tests/test_import_testing.py`, import `field` and extend its existing `FakeCollector` so tests can observe connection specs:

```python
from dataclasses import dataclass, field


@dataclass
class FakeCollector:
    error: Exception | None = None
    seen_devices: list = field(default_factory=list)

    def test_connection(self, device, password):
        self.seen_devices.append(device)
        if self.error:
            raise self.error

    def collect(self, device, password):
        return CollectionResult(())
```

Add the import-test context assertion:

```python
def test_import_test_passes_device_id_to_collector(app):
    batch_id, _, device_id = seed_pending_row(app, "10.0.0.19")
    collector = FakeCollector()
    service = ImportTestService(
        app.state.session_factory,
        app.state.cipher,
        ImmediateExecutor(),
        collector,
        FakeCollector(),
    )

    service.schedule_batch(batch_id)

    assert collector.seen_devices[0].device_id == device_id
```

- [ ] **Step 2: Run the context tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_services.py::test_scan_service_passes_device_id_to_collector tests/test_import_testing.py::test_import_test_passes_device_id_to_collector -v
```

Expected: both tests fail because `DeviceConnectionSpec` has no `device_id` attribute.

- [ ] **Step 3: Add optional device identity and populate it at known call sites**

Change `DeviceConnectionSpec` in `app/collectors/base.py` to:

```python
@dataclass(frozen=True, slots=True)
class DeviceConnectionSpec:
    host: str
    port: int
    username: str
    device_id: int | None = None
```

Build the scan spec in `ScanService.run()` with the database ID:

```python
spec = DeviceConnectionSpec(
    host=device.host,
    port=device.port,
    username=device.username,
    device_id=device.id,
)
```

Extend `ImportTestTarget` and its construction in `app/services/import_testing.py`:

```python
@dataclass(frozen=True)
class ImportTestTarget:
    device_id: int
    os_type: OSType
    host: str
    port: int
    username: str
    encrypted_password: str
```

```python
target = (
    None
    if device is None
    else ImportTestTarget(
        device_id=device.id,
        os_type=device.os_type,
        host=device.host,
        port=device.port,
        username=device.username,
        encrypted_password=device.encrypted_password,
    )
)
```

Pass the ID into the test connection spec:

```python
DeviceConnectionSpec(
    host=target.host,
    port=target.port,
    username=target.username,
    device_id=target.device_id,
)
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_services.py tests/test_import_testing.py -q
```

Expected: all tests in both files pass.

- [ ] **Step 5: Commit the device-context change**

```powershell
git add app/collectors/base.py app/services/scans.py app/services/import_testing.py tests/test_services.py tests/test_import_testing.py
git commit -m "refactor: carry device id into collectors"
```

---

### Task 2: Parse Valid ss Rows Independently From Malformed Rows

**Files:**
- Modify: `app/collectors/linux.py:1-89`
- Test: `tests/test_collectors.py:73-117`

**Interfaces:**
- Consumes: raw `ss` output as `str`.
- Produces: private `SkippedSSLine(line_number: int, reason: str, content: str)`.
- Produces: private `SSParseResult(connections: tuple[NormalizedConnection, ...], skipped_lines: tuple[SkippedSSLine, ...])`.
- Preserves: public `parse_ss_output(output: str) -> tuple[NormalizedConnection, ...]`.

- [ ] **Step 1: Write failing parser tolerance tests**

Add these tests to `tests/test_collectors.py`:

```python
def test_parse_ss_keeps_valid_rows_when_tcp_candidate_is_short():
    rows = parse_ss_output(
        "tcp BROKEN\n"
        "tcp ESTAB 0 0 10.0.0.10:50124 10.0.0.20:443"
    )

    assert len(rows) == 1
    assert rows[0].remote_port == 443


def test_parse_ss_keeps_valid_rows_when_endpoint_is_invalid():
    rows = parse_ss_output(
        "udp UNCONN 0 0 invalid-endpoint 10.0.0.20:53\n"
        "udp UNCONN 0 0 10.0.0.10:5353 10.0.0.20:53"
    )

    assert len(rows) == 1
    assert rows[0].local_port == 5353


def test_parse_ss_ignores_non_connection_text_and_empty_output():
    assert parse_ss_output("") == ()
    assert parse_ss_output("diagnostic text") == ()


def test_parse_ss_fails_when_all_tcp_udp_candidates_are_invalid():
    with pytest.raises(CollectorError, match="无法解析 ss 第 2 行") as captured:
        parse_ss_output("diagnostic text\nudp BROKEN")

    assert captured.value.code == "parse_error"
```

- [ ] **Step 2: Run parser tests and verify the mixed-output cases fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_collectors.py -k "parse_ss" -v
```

Expected: the mixed short-row and invalid-endpoint tests fail under the current fail-fast parser.

- [ ] **Step 3: Implement structured, tolerant parsing**

Import `dataclass`, then add the private result types near the regex constants in `app/collectors/linux.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkippedSSLine:
    line_number: int
    reason: str
    content: str


@dataclass(frozen=True, slots=True)
class SSParseResult:
    connections: tuple[NormalizedConnection, ...]
    skipped_lines: tuple[SkippedSSLine, ...]
```

Replace the fail-fast parser with a detailed parser plus the preserved public wrapper:

```python
def _parse_ss_output_details(output: str) -> SSParseResult:
    rows: list[NormalizedConnection] = []
    skipped: list[SkippedSSLine] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=6)
        protocol_text = parts[0].lower()
        if protocol_text.startswith("tcp"):
            protocol = "tcp"
        elif protocol_text.startswith("udp"):
            protocol = "udp"
        else:
            continue
        if len(parts) < 6:
            skipped.append(SkippedSSLine(line_number, "字段不足", line))
            continue
        state = parts[1].upper() if protocol == "tcp" else None
        try:
            local_ip, local_port = _parse_endpoint(parts[4], remote=False)
            remote_ip, remote_port = _parse_endpoint(parts[5], remote=True)
        except (ValueError, IndexError):
            skipped.append(SkippedSSLine(line_number, "端点无效", line))
            continue

        local_ip = normalize_ip_address(local_ip)
        remote_ip = normalize_ip_address(remote_ip)
        process_name = None
        pid = None
        if len(parts) == 7:
            process_match = _PROCESS_RE.search(parts[6])
            if process_match:
                process_name = process_match.group("name")
                pid = int(process_match.group("pid"))
        assert local_ip is not None and local_port is not None
        rows.append(
            NormalizedConnection(
                protocol=protocol,
                address_family=address_family(local_ip),
                local_ip=local_ip,
                local_port=local_port,
                remote_ip=remote_ip,
                remote_port=remote_port,
                state=state,
                pid=pid,
                process_name=process_name,
            )
        )
    return SSParseResult(tuple(rows), tuple(skipped))


def _raise_if_all_candidates_invalid(parsed: SSParseResult) -> None:
    if parsed.connections or not parsed.skipped_lines:
        return
    first = parsed.skipped_lines[0]
    raise CollectorError("parse_error", f"无法解析 ss 第 {first.line_number} 行")


def parse_ss_output(output: str) -> tuple[NormalizedConnection, ...]:
    parsed = _parse_ss_output_details(output)
    _raise_if_all_candidates_invalid(parsed)
    return parsed.connections
```

- [ ] **Step 4: Run parser tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_collectors.py -k "parse_ss or ipv4_mapped" -v
```

Expected: all selected Linux and Windows normalization/parser tests pass.

- [ ] **Step 5: Commit the tolerant parser**

```powershell
git add app/collectors/linux.py tests/test_collectors.py
git commit -m "fix: tolerate malformed ss rows"
```

---

### Task 3: Log Rejected Rows and Surface Collection Warnings

**Files:**
- Modify: `app/collectors/linux.py:1-5,174-185`
- Test: `tests/test_collectors.py`

**Interfaces:**
- Consumes: `SSParseResult` and `DeviceConnectionSpec.device_id` from Tasks 1 and 2.
- Produces: one warning log per malformed candidate using logger `app.collectors.linux`.
- Produces: `CollectionResult.warning` containing the skipped-row count, merged with the existing fallback warning using `；`.

- [ ] **Step 1: Write failing collector warning and logging tests**

Add this helper and tests to `tests/test_collectors.py`:

```python
class ClosingClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def stub_linux_collection(monkeypatch, collector, responses):
    client = ClosingClient()
    response_iter = iter(responses)
    monkeypatch.setattr(collector, "_connect", lambda device, password: client)
    monkeypatch.setattr(
        collector,
        "_execute",
        lambda connected_client, command: next(response_iter),
    )
    return client


def test_linux_collect_logs_raw_skipped_line_and_returns_warning(monkeypatch, caplog):
    collector = LinuxCollector()
    malformed = "tcp BROKEN"
    client = stub_linux_collection(
        monkeypatch,
        collector,
        [(0, f"{malformed}\ntcp ESTAB 0 0 10.0.0.10:22 10.0.0.20:443", "")],
    )

    with caplog.at_level("WARNING", logger="app.collectors.linux"):
        result = collector.collect(
            DeviceConnectionSpec("10.0.0.10", 22, "ops", device_id=42),
            "secret",
        )

    assert len(result.connections) == 1
    assert result.warning == "已跳过 1 条无法解析的 ss 记录"
    assert "设备 42" in caplog.text
    assert "ss 第 1 行" in caplog.text
    assert "字段不足" in caplog.text
    assert repr(malformed) in caplog.text
    assert client.closed is True


def test_linux_collect_merges_fallback_and_parse_warnings(monkeypatch, caplog):
    collector = LinuxCollector()
    stub_linux_collection(
        monkeypatch,
        collector,
        [
            (1, "", "permission denied"),
            (0, "udp BROKEN\nudp UNCONN 0 0 10.0.0.10:53 10.0.0.20:53", ""),
        ],
    )

    with caplog.at_level("WARNING", logger="app.collectors.linux"):
        result = collector.collect(
            DeviceConnectionSpec("10.0.0.10", 22, "ops"),
            "secret",
        )

    assert result.warning == (
        "当前账户无法读取完整进程信息，已降级采集网络连接；"
        "已跳过 1 条无法解析的 ss 记录"
    )
    assert "设备 unknown" in caplog.text


def test_linux_collect_logs_before_failing_all_invalid_output(monkeypatch, caplog):
    collector = LinuxCollector()
    stub_linux_collection(monkeypatch, collector, [(0, "udp BROKEN", "")])

    with caplog.at_level("WARNING", logger="app.collectors.linux"):
        with pytest.raises(CollectorError, match="无法解析 ss 第 1 行"):
            collector.collect(
                DeviceConnectionSpec("10.0.0.10", 22, "ops", device_id=7),
                "secret",
            )

    assert "设备 7" in caplog.text
    assert repr("udp BROKEN") in caplog.text
```

Also import the connection spec:

```python
from app.collectors.base import CollectorError, DeviceConnectionSpec
```

- [ ] **Step 2: Run the collector integration tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_collectors.py -k "logs_raw or merges_fallback or logs_before" -v
```

Expected: tests fail because `collect()` still delegates directly to `parse_ss_output()` and emits neither row logs nor skipped-row warnings.

- [ ] **Step 3: Log every rejected row and merge warnings**

Add module logging setup near the imports in `app/collectors/linux.py`:

```python
import logging

logger = logging.getLogger(__name__)
```

Add the rejected-row logger:

```python
def _log_skipped_lines(device: DeviceConnectionSpec, parsed: SSParseResult) -> None:
    device_label = device.device_id if device.device_id is not None else "unknown"
    for skipped in parsed.skipped_lines:
        logger.warning(
            "设备 %s 跳过无法解析的 ss 第 %s 行 reason=%s raw=%r",
            device_label,
            skipped.line_number,
            skipped.reason,
            skipped.content,
        )
```

Replace the end of `LinuxCollector.collect()` with detailed parsing, logging, validation, and warning merging:

```python
            warnings: list[str] = []
            if code != 0:
                fallback_code, output, fallback_error = self._execute(
                    client, SS_WITHOUT_PROCESS
                )
                if fallback_code != 0:
                    detail = fallback_error.strip() or error.strip() or "服务器未安装 ss"
                    raise CollectorError("command_unavailable", detail)
                warnings.append("当前账户无法读取完整进程信息，已降级采集网络连接")
            parsed = _parse_ss_output_details(output)
            _log_skipped_lines(device, parsed)
            _raise_if_all_candidates_invalid(parsed)
            if parsed.skipped_lines:
                warnings.append(
                    f"已跳过 {len(parsed.skipped_lines)} 条无法解析的 ss 记录"
                )
            return CollectionResult(
                parsed.connections,
                "；".join(warnings) if warnings else None,
            )
```

- [ ] **Step 4: Run collector tests, lint, and the complete suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_collectors.py -v
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: collector tests pass; Ruff reports no errors; the complete test suite passes.

- [ ] **Step 5: Commit the warning and logging integration**

```powershell
git add app/collectors/linux.py tests/test_collectors.py
git commit -m "fix: log skipped ss records"
```

---

### Task 4: Final Regression and Scope Verification

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: completed changes from Tasks 1-3.
- Produces: evidence that parsing, service propagation, lint, and full regression requirements pass together.

- [ ] **Step 1: Verify the committed diff contains only in-scope files**

Run:

```powershell
git status --short
git diff --stat a54d03f..HEAD
git log --oneline a54d03f..HEAD
```

Expected: the worktree is clean; changes are limited to collector context, Linux parsing, their service call sites, and tests; three implementation commits are present.

- [ ] **Step 2: Run final verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
```

Expected: Ruff passes, the full pytest suite passes, and coverage completes without failure.

- [ ] **Step 3: Record the verified commit**

Run:

```powershell
git rev-parse --short HEAD
```

Expected: prints the final implementation commit ID for the handoff report.
