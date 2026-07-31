# Optional Windows Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the application to install and start without `pywinrm`, while preserving an opt-in Windows scanning capability.

**Architecture:** Move `pywinrm` into a `windows` optional dependency group and guard its module import inside the Windows collector. Keep `WindowsCollector` importable in every environment; when no session factory can be resolved, raise the existing `CollectorError` before any network operation.

**Tech Stack:** Python 3.10, FastAPI, Paramiko, optional pywinrm, pytest, Ruff

## Global Constraints

- Python must remain `>=3.10,<3.11`.
- Missing `pywinrm` must not prevent project installation, module import, or FastAPI startup.
- Linux SSH collection must remain available without `pywinrm`.
- Windows devices remain storable; their tests and scans fail with a persisted Chinese capability message.
- Do not automatically install packages or access the internet at runtime.
- Do not perform Git operations for this implementation.

---

### Task 1: Make the dependency optional

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Produces: default installation command `pip install -e .`
- Produces: Windows-enabled installation command `pip install -e ".[windows]"`

- [ ] **Step 1: Move the dependency**

Remove `"pywinrm>=0.5,<1"` from `[project].dependencies` and add:

```toml
[project.optional-dependencies]
windows = [
  "pywinrm>=0.5,<1",
]
```

Keep the existing `test` extra in the same table.

- [ ] **Step 2: Document both installation modes**

Add README instructions explaining that the base install supports Linux collection and that the `windows` extra enables WinRM. State that missing `pywinrm` does not prevent startup.

- [ ] **Step 3: Validate project metadata**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Expected: editable metadata builds successfully without resolving `pywinrm`.

### Task 2: Add the unavailable-component fallback

**Files:**
- Modify: `app/collectors/windows.py`
- Test: `tests/test_windows_optional.py`

**Interfaces:**
- Consumes: `CollectorError(code: str, message: str)`
- Produces: `WINDOWS_COMPONENT_ERROR_CODE = "windows_component_unavailable"`
- Produces: `WINDOWS_COMPONENT_ERROR_MESSAGE = "当前环境未安装 Windows 采集组件，请安装 pywinrm"`
- Produces: `WindowsCollector(timeout: int = 15, session_factory: Callable[..., object] | None = None)`

- [ ] **Step 1: Write failing fallback tests**

Create tests which monkeypatch the Windows collector module's `winrm` binding to
`None`, instantiate `WindowsCollector`, and assert both `test_connection()` and
`collect()` raise `CollectorError` with the exact code and Chinese message.
Also assert `parse_windows_json()` remains usable because parsing does not depend
on WinRM.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_windows_optional.py -q
```

Expected: failure because `winrm` is currently imported unconditionally and the
fallback constants do not exist.

- [ ] **Step 3: Guard the optional import**

Use:

```python
try:
    import winrm
    from winrm.exceptions import (
        InvalidCredentialsError,
        WinRMOperationTimeoutError,
        WinRMTransportError,
    )
except ModuleNotFoundError:
    winrm = None
    InvalidCredentialsError = ()
    WinRMOperationTimeoutError = ()
    WinRMTransportError = ()
```

Change `WindowsCollector` so a supplied test session factory still works, while
the default factory is resolved lazily. Before creating a session, raise:

```python
CollectorError(
    WINDOWS_COMPONENT_ERROR_CODE,
    WINDOWS_COMPONENT_ERROR_MESSAGE,
)
```

when `winrm` is unavailable.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_windows_optional.py tests\test_collectors.py -q
```

Expected: all focused tests pass.

### Task 3: Verify startup and scan integration

**Files:**
- Test: `tests/test_windows_optional.py`
- Verify: `app/main.py`
- Verify: `app/services/scans.py`

**Interfaces:**
- Consumes: the standard `Collector` protocol
- Produces: normal application startup without `pywinrm`
- Produces: failed Windows scan records with error code `windows_component_unavailable`

- [ ] **Step 1: Add integration tests**

Add a test that creates a Windows device, injects an unavailable
`WindowsCollector`, runs `ScanService.run()`, and verifies:

```python
run.status == ScanStatus.FAILED
run.error_code == "windows_component_unavailable"
run.error_message == "当前环境未安装 Windows 采集组件，请安装 pywinrm"
```

Add an application creation test with the module's `winrm` binding set to
`None`, verifying `create_app()` completes and its Linux collector remains
initialized.

- [ ] **Step 2: Run integration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_windows_optional.py -q
```

Expected: all optional-component and integration tests pass.

- [ ] **Step 3: Run the complete verification suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: Ruff reports no errors and all tests pass.

- [ ] **Step 4: Validate a true no-pywinrm import**

Run a Python process that temporarily blocks imports whose names start with
`winrm`, then imports `app.main` and creates the application.

Expected: process exits successfully and prints a confirmation that the app was
created without the Windows component.

