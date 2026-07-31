# Linux Deployment Package Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a repeatable PowerShell script that creates a clean, fixed-name Linux deployment archive without the existing SQLite database.

**Architecture:** A single root-level PowerShell script resolves paths relative to itself, validates required inputs, invokes the system `tar`, and validates the resulting archive. It includes `wheelhouse/` only when present and removes partial output after any failure.

**Tech Stack:** PowerShell 5.1+, Windows `tar`/bsdtar, gzip-compressed tar archive

## Global Constraints

- Output must always be `connection-topology-linux.tar.gz` in the project root and replace the previous output.
- Required inputs are `app/`, `pyproject.toml`, `.env`, `.env.example`, and `README.md`.
- `connection_topology.db` must not be included; Linux creates a new empty database at first startup.
- `wheelhouse/` is optional and must be included automatically when present.
- Exclude `__pycache__/`, `*.pyc`, `*.pyo`, `*.log`, `.pytest_cache/`, and `.ruff_cache/`.
- On failure, exit nonzero and remove any incomplete output archive.
- The archive contains `.env` and must be treated as sensitive.

---

### Task 1: Implement and verify the Linux packaging script

**Files:**
- Create: `package-linux.ps1`

**Interfaces:**
- Consumes: project files and directories listed in Global Constraints; the `tar` command on `PATH`
- Produces: `connection-topology-linux.tar.gz` in the project root and process exit code `0` on success

- [ ] **Step 1: Create a failing smoke check**

Run before the script exists:

```powershell
Test-Path .\package-linux.ps1
```

Expected: `False`.

- [ ] **Step 2: Implement `package-linux.ps1`**

Create the file with this complete content:

```powershell
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$archiveName = "connection-topology-linux.tar.gz"
$archivePath = Join-Path $projectRoot $archiveName
$requiredPaths = @(
    "app",
    "pyproject.toml",
    ".env",
    ".env.example",
    "README.md"
)
$optionalPath = "wheelhouse"

Set-Location -LiteralPath $projectRoot

if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "未找到 tar 命令。请安装或启用 Windows 自带的 bsdtar 后重试。"
}

$missingPaths = @(
    $requiredPaths | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $projectRoot $_))
    }
)
if ($missingPaths.Count -gt 0) {
    throw "缺少必须打包的路径：$($missingPaths -join ', ')"
}

$packagePaths = [System.Collections.Generic.List[string]]::new()
foreach ($path in $requiredPaths) {
    $packagePaths.Add($path)
}

$includesWheelhouse = Test-Path -LiteralPath (Join-Path $projectRoot $optionalPath)
if ($includesWheelhouse) {
    $packagePaths.Add($optionalPath)
} else {
    Write-Host "提示：未找到 wheelhouse，部署包将不包含离线依赖。"
}

if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}

$tarArguments = @(
    "--exclude=*/__pycache__"
    "--exclude=*.pyc"
    "--exclude=*.pyo"
    "--exclude=*.log"
    "--exclude=*/.pytest_cache"
    "--exclude=*/.ruff_cache"
    "-czf"
    $archivePath
) + $packagePaths.ToArray()

try {
    & tar @tarArguments
    if ($LASTEXITCODE -ne 0) {
        throw "tar 打包失败，退出码：$LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "tar 未生成预期的压缩包。"
    }

    $archiveEntries = @(& tar -tzf $archivePath)
    if ($LASTEXITCODE -ne 0) {
        throw "生成的压缩包无法读取，tar 退出码：$LASTEXITCODE"
    }

    foreach ($requiredPath in $requiredPaths) {
        $entryPrefix = $requiredPath.Replace("\", "/")
        $found = @(
            $archiveEntries | Where-Object {
                $_ -eq $entryPrefix -or $_ -eq "$entryPrefix/" -or
                $_.StartsWith("$entryPrefix/")
            }
        ).Count -gt 0
        if (-not $found) {
            throw "压缩包缺少必须内容：$requiredPath"
        }
    }

    $forbiddenPattern = '(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)(/|$)|\.(pyc|pyo|log)$'
    $forbiddenEntries = @($archiveEntries | Where-Object { $_ -match $forbiddenPattern })
    if ($forbiddenEntries.Count -gt 0) {
        throw "压缩包包含应排除的缓存或日志：$($forbiddenEntries -join ', ')"
    }
    if (@($archiveEntries | Where-Object { $_ -eq "connection_topology.db" }).Count -gt 0) {
        throw "压缩包意外包含 connection_topology.db。"
    }

    $archive = Get-Item -LiteralPath $archivePath
    $sizeMiB = [Math]::Round($archive.Length / 1MB, 2)
    Write-Host "打包完成：$($archive.FullName)"
    Write-Host "文件大小：$sizeMiB MiB"
    Write-Host "包含 wheelhouse：$includesWheelhouse"
    Write-Warning "压缩包包含 .env，请通过安全方式传输和保存。"
} catch {
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    throw
}
```

- [ ] **Step 3: Run the script**

```powershell
& .\package-linux.ps1
```

Expected: exit code `0`; output reports the absolute archive path, its size, whether
`wheelhouse` was included, and a warning about `.env`.

- [ ] **Step 4: Verify archive contents and exclusions**

```powershell
$entries = @(tar -tzf .\connection-topology-linux.tar.gz)
$LASTEXITCODE
$entries | Where-Object {
    $_ -match '(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)(/|$)|\.(pyc|pyo|log)$'
}
$entries | Where-Object { $_ -eq 'connection_topology.db' }
$entries | Select-String -Pattern '^app/|^pyproject.toml$|^\.env$|^\.env.example$|^README.md$'
```

Expected: exit code `0`; the two exclusion queries produce no output; the final query
shows entries for all required inputs.

- [ ] **Step 5: Verify missing-input failure in an isolated fixture**

```powershell
$fixture = Join-Path $env:TEMP "connection-topology-package-test"
if (Test-Path -LiteralPath $fixture) {
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
New-Item -ItemType Directory -Path $fixture | Out-Null
Copy-Item .\package-linux.ps1 $fixture
Push-Location $fixture
try {
    & .\package-linux.ps1
    $fixtureExitCode = $LASTEXITCODE
} catch {
    $fixtureExitCode = 1
}
Pop-Location
$fixtureExitCode
Test-Path (Join-Path $fixture "connection-topology-linux.tar.gz")
Remove-Item -LiteralPath $fixture -Recurse -Force
```

Expected: `1`, then `False`.

- [ ] **Step 6: Check PowerShell syntax**

```powershell
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path .\package-linux.ps1),
    [ref]$null,
    [ref]$errors
) | Out-Null
$errors
```

Expected: no output.

- [ ] **Step 7: Commit the implementation**

```powershell
git add package-linux.ps1
git commit -m "build: add Linux deployment packaging script"
```

Expected: commit succeeds after repository-local or global Git author identity is configured.
