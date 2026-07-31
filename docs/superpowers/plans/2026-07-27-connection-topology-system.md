# Server Connection Topology System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python 3.10 FastAPI application that securely stores Linux SSH and Windows WinRM devices, periodically captures their TCP/UDP connections, retains snapshot history, and displays the latest data as an interactive topology.

**Architecture:** A single FastAPI process serves server-rendered pages and JSON endpoints, persists state in SQLite through SQLAlchemy, and runs APScheduler jobs. OS-specific collectors normalize remote command output into one domain model; scan orchestration owns locking and transactional snapshot writes.

**Tech Stack:** Python 3.10, FastAPI, Jinja2, HTMX, Cytoscape.js, SQLAlchemy 2.x, APScheduler, Paramiko, pywinrm, cryptography/Fernet, pytest

## Global Constraints

- Python runtime must be exactly Python 3.10 for the project virtual environment.
- The default bind address is `127.0.0.1`.
- Remote passwords are encrypted with `APP_SECRET_KEY` and are never logged or returned.
- Linux uses SSH and fixed `ss` commands; Windows uses WinRM and fixed PowerShell.
- Each device has an independent interval of at least 1 minute, defaulting to 5 minutes.
- Successful and failed scan runs are retained; successful connection snapshots default to 30 days.
- Automated tests use fake remote clients and require no real servers.
- The current release is a single-administrator tool without application login.

---

## File Structure

- `pyproject.toml`: package metadata, Python constraint, runtime and test dependencies.
- `.env.example`: non-secret local configuration template.
- `README.md`: installation, security, WinRM/SSH prerequisites, run and test instructions.
- `app/config.py`: validated environment configuration.
- `app/database.py`: SQLAlchemy engine, session factory, schema initialization.
- `app/models.py`: device, scan, connection, and setting ORM models.
- `app/schemas.py`: request validation and API response schemas.
- `app/security.py`: Fernet encryption and safe error messages.
- `app/collectors/base.py`: normalized connection type, collector protocol, collection exceptions.
- `app/collectors/linux.py`: Paramiko execution and `ss` parser.
- `app/collectors/windows.py`: WinRM execution and PowerShell JSON parser.
- `app/services/scans.py`: device lock, scan lifecycle, transactional persistence.
- `app/services/topology.py`: topology aggregation and snapshot diff.
- `app/services/scheduler.py`: APScheduler registration and retention job.
- `app/routes/api.py`: device, scan, history, topology, and settings JSON endpoints.
- `app/routes/pages.py`: Jinja page routes.
- `app/main.py`: application factory and lifespan.
- `app/templates/*.html`: dashboard, topology, device, history, and settings pages.
- `app/static/css/app.css`: industrial operations-console visual system.
- `app/static/js/topology.js`: Cytoscape rendering and detail interactions.
- `tests/`: parser, security, scan, topology, API, and page-flow tests.

### Task 1: Python 3.10 Environment and Application Foundation

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/database.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings`, `get_settings()`, `Base`, `SessionLocal`, `get_db()`, `init_database()`.

- [ ] **Step 1: Create Python 3.10 virtual environment**

Run:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

Expected: `.\.venv\Scripts\python.exe --version` prints `Python 3.10.x`.

- [ ] **Step 2: Define package metadata and dependencies**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "connection-topology"
version = "0.1.0"
requires-python = ">=3.10,<3.11"
dependencies = [
  "fastapi>=0.116,<1",
  "uvicorn[standard]>=0.35,<1",
  "sqlalchemy>=2.0,<3",
  "jinja2>=3.1,<4",
  "python-multipart>=0.0.20,<1",
  "apscheduler>=3.11,<4",
  "paramiko>=3.5,<4",
  "pywinrm>=0.5,<1",
  "cryptography>=45,<46",
  "pydantic-settings>=2.10,<3",
]

[project.optional-dependencies]
test = [
  "httpx>=0.28,<1",
  "pytest>=8.4,<9",
  "pytest-cov>=6.2,<7",
  "ruff>=0.12,<1",
]

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
target-version = "py310"
line-length = 100
```

Create `.env.example`:

```dotenv
APP_SECRET_KEY=replace-with-a-generated-fernet-key
DATABASE_URL=sqlite:///./connection_topology.db
HOST=127.0.0.1
PORT=8000
HISTORY_RETENTION_DAYS=30
```

- [ ] **Step 3: Write failing configuration tests**

```python
def test_settings_reject_missing_secret(monkeypatch):
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_defaults_to_loopback(valid_key):
    settings = Settings(APP_SECRET_KEY=valid_key, _env_file=None)
    assert settings.host == "127.0.0.1"
    assert settings.history_retention_days == 30
```

- [ ] **Step 4: Run the tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q`

Expected: FAIL because `app.config` does not exist.

- [ ] **Step 5: Implement validated configuration and database setup**

```python
class Settings(BaseSettings):
    app_secret_key: str = Field(min_length=44)
    database_url: str = "sqlite:///./connection_topology.db"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    history_retention_days: int = Field(default=30, ge=1)
```

Use SQLite `check_same_thread=False`; expose a generator-based `get_db`; make `init_database()` call `Base.metadata.create_all`.

- [ ] **Step 6: Install and verify**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q
```

Expected: all configuration tests PASS.

### Task 2: Persistence and Credential Protection

**Files:**
- Create: `app/models.py`
- Create: `app/schemas.py`
- Create: `app/security.py`
- Create: `tests/test_security.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: `Settings.app_secret_key`, `Base`.
- Produces: `Device`, `ScanRun`, `ConnectionRecord`, `SystemSetting`, `CredentialCipher.encrypt(str) -> str`, `CredentialCipher.decrypt(str) -> str`, and validated device/settings schemas.

- [ ] **Step 1: Write failing encryption and model tests**

```python
def test_cipher_round_trip(valid_key):
    cipher = CredentialCipher(valid_key)
    token = cipher.encrypt("S3cret!")
    assert token != "S3cret!"
    assert cipher.decrypt(token) == "S3cret!"


def test_device_identity_is_unique(db_session):
    db_session.add_all([device(host="10.0.0.1"), device(host="10.0.0.1")])
    with pytest.raises(IntegrityError):
        db_session.commit()
```

- [ ] **Step 2: Verify failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_security.py tests/test_models.py -q`

Expected: FAIL because the models and cipher are missing.

- [ ] **Step 3: Implement ORM entities and cascades**

Define enums for OS type, scan trigger, and scan status. Make `(host, port, username)` unique, configure `ScanRun.connections` with `cascade="all, delete-orphan"`, and index scan/filter columns.

- [ ] **Step 4: Implement credential cipher and schemas**

```python
class CredentialCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
```

Device response schemas must omit `encrypted_password`. Device updates interpret an empty password as “keep existing”.

- [ ] **Step 5: Verify persistence and security**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_security.py tests/test_models.py -q`

Expected: all tests PASS.

### Task 3: Cross-Platform Connection Collectors

**Files:**
- Create: `app/collectors/__init__.py`
- Create: `app/collectors/base.py`
- Create: `app/collectors/linux.py`
- Create: `app/collectors/windows.py`
- Create: `tests/fixtures/linux_ss.txt`
- Create: `tests/fixtures/windows_connections.json`
- Create: `tests/test_linux_collector.py`
- Create: `tests/test_windows_collector.py`

**Interfaces:**
- Produces: immutable `NormalizedConnection`, `CollectionResult`, `CollectorError`, `LinuxCollector.collect(device, password)`, `WindowsCollector.collect(device, password)`, `parse_ss_output(str)`, and `parse_windows_json(str)`.

- [ ] **Step 1: Add representative parser fixtures and failing tests**

```python
def test_parse_ss_tcp_with_process(linux_ss_output):
    rows = parse_ss_output(linux_ss_output)
    row = next(item for item in rows if item.remote_port == 443)
    assert row.protocol == "tcp"
    assert row.process_name == "curl"
    assert row.pid == 912


def test_parse_windows_udp_without_remote_endpoint(windows_output):
    rows = parse_windows_json(windows_output)
    udp = next(item for item in rows if item.protocol == "udp")
    assert udp.remote_ip is None
    assert udp.state is None
```

- [ ] **Step 2: Verify parser failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_linux_collector.py tests/test_windows_collector.py -q`

Expected: FAIL because collector modules do not exist.

- [ ] **Step 3: Implement normalized types and parsers**

```python
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
```

Parse bracketed IPv6 and wildcard endpoints without splitting on the first colon. Treat `*:*`, `0.0.0.0:0`, and UDP’s absent peer as no remote endpoint.

- [ ] **Step 4: Implement fixed remote commands**

Linux must execute only the constant `ss -H -tunap` and retry with `ss -H -tuna` when process metadata is unavailable. Windows must execute one constant PowerShell script that emits compressed JSON. Map timeout, authentication, transport, command, and parse errors to stable `CollectorError.code` values.

- [ ] **Step 5: Verify collectors**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_linux_collector.py tests/test_windows_collector.py -q`

Expected: all parser and fake-client tests PASS.

### Task 4: Scan Lifecycle, Retention, and Topology Domain Logic

**Files:**
- Create: `app/services/__init__.py`
- Create: `app/services/scans.py`
- Create: `app/services/topology.py`
- Create: `app/services/scheduler.py`
- Create: `tests/test_scan_service.py`
- Create: `tests/test_topology_service.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: collectors, ORM models, `CredentialCipher`.
- Produces: `ScanService.test_connection(device, password)`, `ScanService.run(device_id, trigger)`, `build_topology(scan_run)`, `diff_scans(previous, current)`, `configure_scheduler(app)`, and `purge_expired_scans(session, retention_days)`.

- [ ] **Step 1: Write failing transactional scan tests**

```python
def test_failed_scan_has_no_partial_connections(scan_service, failing_collector, db_session):
    run = scan_service.run(device_id=1, trigger=ScanTrigger.MANUAL)
    assert run.status == ScanStatus.FAILED
    assert db_session.query(ConnectionRecord).count() == 0


def test_same_device_scan_is_not_concurrent(scan_service):
    with scan_service.lock_for(1):
        with pytest.raises(ScanAlreadyRunning):
            scan_service.run(1, ScanTrigger.MANUAL)
```

- [ ] **Step 2: Verify service failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scan_service.py tests/test_topology_service.py tests/test_scheduler.py -q`

Expected: FAIL because service modules are absent.

- [ ] **Step 3: Implement scan orchestration**

Maintain one `threading.Lock` per device. Create the running row before remote I/O, stage normalized rows in memory, then insert all connection records and mark success in one transaction. On error, roll back staged records and commit only a failed run with a redacted message of at most 500 characters.

- [ ] **Step 4: Implement topology and diff**

Aggregate remote endpoints by IP. Omit UDP listeners and TCP listeners without remote peers from topology edges, but keep them available in detail tables. Diff on:

```python
(protocol, local_ip, local_port, remote_ip, remote_port, pid, process_name)
```

- [ ] **Step 5: Implement scheduler and retention**

Register stable job IDs `device-scan-{id}` with `max_instances=1`, `coalesce=True`, and per-device minute intervals. Register one daily retention job. Removing or disabling a device removes its job.

- [ ] **Step 6: Verify domain services**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scan_service.py tests/test_topology_service.py tests/test_scheduler.py -q`

Expected: all tests PASS.

### Task 5: FastAPI Endpoints and Application Lifespan

**Files:**
- Create: `app/routes/__init__.py`
- Create: `app/routes/api.py`
- Create: `app/routes/pages.py`
- Create: `app/main.py`
- Create: `tests/test_api.py`

**Interfaces:**
- Consumes: schemas, services, scheduler, database.
- Produces: `create_app(settings: Settings | None = None) -> FastAPI` and routes under `/api`.

- [ ] **Step 1: Write failing API workflow tests**

```python
def test_create_device_never_returns_password(client, fake_collectors):
    response = client.post("/api/devices", json=LINUX_DEVICE)
    assert response.status_code == 201
    body = response.json()
    assert "password" not in body
    assert "encrypted_password" not in body


def test_manual_scan_returns_conflict_when_running(client, locked_device):
    response = client.post(f"/api/devices/{locked_device.id}/scan")
    assert response.status_code == 409
```

- [ ] **Step 2: Verify endpoint failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_api.py -q`

Expected: FAIL because the application factory is missing.

- [ ] **Step 3: Implement JSON endpoints**

Provide:

- `GET/POST /api/devices`
- `PUT/DELETE /api/devices/{id}`
- `POST /api/devices/test`
- `POST /api/devices/{id}/scan`
- `GET /api/scans`
- `GET /api/scans/{id}`
- `GET /api/scans/{id}/topology`
- `GET /api/scans/{id}/diff`
- `GET/PUT /api/settings`

Return 422 for validation, 404 for missing rows, 409 for duplicate devices or an already-running scan, and 502 for remote connection test failures.

- [ ] **Step 4: Implement lifespan**

At startup validate the secret, initialize tables and default setting, start the scheduler, and synchronize enabled device jobs. At shutdown stop the scheduler without waiting for remote tasks.

- [ ] **Step 5: Verify the API**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_api.py -q`

Expected: all endpoint tests PASS.

### Task 6: Topology-First Operations Interface

**Files:**
- Create: `app/templates/base.html`
- Create: `app/templates/dashboard.html`
- Create: `app/templates/topology.html`
- Create: `app/templates/devices.html`
- Create: `app/templates/history.html`
- Create: `app/templates/settings.html`
- Create: `app/static/css/app.css`
- Create: `app/static/js/topology.js`
- Create: `tests/test_pages.py`

**Interfaces:**
- Consumes: page routes and `/api/scans/{id}/topology`.
- Produces: accessible server-rendered pages at `/`, `/topology`, `/devices`, `/history`, and `/settings`.

- [ ] **Step 1: Write failing page tests**

```python
@pytest.mark.parametrize("path", ["/", "/topology", "/devices", "/history", "/settings"])
def test_page_routes_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "CONNECTION ATLAS" in response.text
```

- [ ] **Step 2: Verify page failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_pages.py -q`

Expected: FAIL because templates are absent.

- [ ] **Step 3: Build the interface**

Use an industrial “network operations atlas” direction: near-black blue background, phosphor green status accents, warm amber warnings, hairline grids, compact monospace telemetry labels, and a readable Chinese UI body face. Avoid decorative gradients and generic card grids. Include visible keyboard focus, semantic labels, reduced-motion support, and responsive fallback below 900 px.

- [ ] **Step 4: Implement topology interaction**

Initialize Cytoscape from the topology endpoint, style server and peer nodes distinctly, show edge counts, fit the graph, and populate a detail drawer when a node or edge is selected. Filter controls refresh the graph without a full page reload.

- [ ] **Step 5: Verify pages**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_pages.py -q`

Expected: all page route and key-content tests PASS.

### Task 7: Documentation and End-to-End Verification

**Files:**
- Create: `README.md`
- Create: `tests/test_redaction.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: the complete application.
- Produces: reproducible Python 3.10 setup and operator instructions.

- [ ] **Step 1: Add security regression tests**

```python
def test_remote_password_not_present_in_logs(client, caplog, failing_secret_device):
    client.post("/api/devices/test", json=failing_secret_device)
    assert failing_secret_device["password"] not in caplog.text
```

- [ ] **Step 2: Write operator documentation**

Document:

- Python 3.10 virtual environment creation.
- Fernet key generation and `.env` setup.
- Linux SSH and `ss` prerequisites.
- Windows WinRM enablement, ports, and trusted-host considerations.
- Development start command.
- Database location and backup.
- Test and lint commands.
- The security implication of binding beyond loopback.

- [ ] **Step 3: Run all automated checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
```

Expected: lint succeeds and all tests PASS.

- [ ] **Step 4: Perform local smoke test**

Run:

```powershell
$env:APP_SECRET_KEY = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, verify all five pages load, and confirm the API documents load at `/docs`.

- [ ] **Step 5: Confirm deliverables**

Verify `.venv` uses Python 3.10, no secret or database file is tracked, the README commands work from a clean shell, and `git status --short` contains only intentional project files.
