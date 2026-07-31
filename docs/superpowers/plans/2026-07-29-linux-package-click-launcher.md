# Linux Deployment Package Click Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Windows command file that runs the existing Linux packaging script by double-click without requiring the user to type a PowerShell command.

**Architecture:** A root-level `package-linux.cmd` acts only as a launcher around `package-linux.ps1`. It resolves the PowerShell script relative to itself, invokes it with a process-local execution-policy bypass, reports success or failure, pauses for double-click visibility, and returns the PowerShell exit code.

**Tech Stack:** Windows command processor (`cmd.exe`), Windows PowerShell, existing `package-linux.ps1`

## Global Constraints

- The launcher must be named `package-linux.cmd` and live beside `package-linux.ps1`.
- It must work regardless of the caller's current directory.
- It must not modify machine-level or user-level PowerShell execution policy.
- Missing `package-linux.ps1` and PowerShell failures must produce a nonzero exit code.
- The console must pause after success or failure so double-click users can read the result.
- Existing archive creation and validation remain solely in `package-linux.ps1`.

---

### Task 1: Add and verify the double-click launcher

**Files:**
- Create: `package-linux.cmd`

**Interfaces:**
- Consumes: `package-linux.ps1` in the launcher's directory and `powershell.exe` on `PATH`
- Produces: the same `connection-topology-linux.tar.gz` produced by `package-linux.ps1`; exit code `0` on success and nonzero on failure

- [ ] **Step 1: Confirm the launcher does not exist**

```powershell
Test-Path .\package-linux.cmd
```

Expected: `False`.

- [ ] **Step 2: Create `package-linux.cmd`**

```bat
@echo off
setlocal

set "POWERSHELL_SCRIPT=%~dp0package-linux.ps1"

if not exist "%POWERSHELL_SCRIPT%" (
    echo ERROR: package-linux.ps1 was not found beside this launcher.
    set "EXIT_CODE=1"
    goto :finish
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%POWERSHELL_SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"

if "%EXIT_CODE%"=="0" (
    echo.
    echo Linux deployment package created successfully.
) else (
    echo.
    echo ERROR: Packaging failed with exit code %EXIT_CODE%.
)

:finish
echo.
pause
exit /b %EXIT_CODE%
```

- [ ] **Step 3: Run the launcher from the project root**

```powershell
cmd.exe /d /c "(echo.|package-linux.cmd)"
$LASTEXITCODE
```

Expected: output contains `Linux deployment package created successfully.` and the
reported exit code is `0`.

- [ ] **Step 4: Run the launcher from another working directory**

```powershell
$launcher = (Resolve-Path .\package-linux.cmd).Path
Push-Location $env:TEMP
try {
    cmd.exe /d /c "(echo.|`"$launcher`")"
    $launcherExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
$launcherExitCode
```

Expected: packaging succeeds and the reported exit code is `0`.

- [ ] **Step 5: Verify the missing-script failure path**

```powershell
$fixture = Join-Path $env:TEMP "connection-topology-launcher-test"
if (Test-Path -LiteralPath $fixture) {
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
New-Item -ItemType Directory -Path $fixture | Out-Null
Copy-Item .\package-linux.cmd $fixture

cmd.exe /d /c "(echo.|`"$fixture\package-linux.cmd`")"
$fixtureExitCode = $LASTEXITCODE

$fixtureExitCode
Remove-Item -LiteralPath $fixture -Recurse -Force
```

Expected: output contains `package-linux.ps1 was not found`, and the reported exit
code is `1`.

- [ ] **Step 6: Verify the generated archive remains valid**

```powershell
$entries = @(tar -tzf .\connection-topology-linux.tar.gz)
if ($LASTEXITCODE -ne 0) {
    throw "Generated archive is unreadable."
}
$forbidden = @(
    $entries | Where-Object {
        $_ -match '(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)(/|$)|\.(pyc|pyo|log)$' -or
        $_ -eq 'connection_topology.db'
    }
)
$forbidden
```

Expected: the archive is readable and the forbidden-entry query produces no output.

- [ ] **Step 7: Commit the launcher**

```powershell
git add package-linux.cmd
git commit -m "build: add click-to-run packaging launcher"
```

Expected: commit succeeds after Git author identity is configured.
