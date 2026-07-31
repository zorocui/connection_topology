[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$beijingNow = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(8))
$timestamp = $beijingNow.ToString("yyyyMMdd-HHmmss")
$archiveName = "connection-topology-linux-$timestamp.tar.gz"
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
    throw "The tar command was not found. Install or enable Windows bsdtar and retry."
}

$missingPaths = @(
    $requiredPaths | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $projectRoot $_))
    }
)
if ($missingPaths.Count -gt 0) {
    throw "Required package paths are missing: $($missingPaths -join ', ')"
}

$packagePaths = [System.Collections.Generic.List[string]]::new()
foreach ($path in $requiredPaths) {
    $packagePaths.Add($path)
}

$includesWheelhouse = Test-Path -LiteralPath (Join-Path $projectRoot $optionalPath)
if ($includesWheelhouse) {
    $packagePaths.Add($optionalPath)
} else {
    Write-Host "Note: wheelhouse was not found; offline dependencies will not be included."
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
        throw "tar failed with exit code $LASTEXITCODE."
    }
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        throw "tar did not create the expected archive."
    }

    $archiveEntries = @(& tar -tzf $archivePath)
    if ($LASTEXITCODE -ne 0) {
        throw "The generated archive is unreadable; tar exited with code $LASTEXITCODE."
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
            throw "The archive is missing required content: $requiredPath"
        }
    }

    $forbiddenPattern = '(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)(/|$)|\.(pyc|pyo|log)$'
    $forbiddenEntries = @($archiveEntries | Where-Object { $_ -match $forbiddenPattern })
    if ($forbiddenEntries.Count -gt 0) {
        throw "The archive contains excluded cache or log files: $($forbiddenEntries -join ', ')"
    }
    if (@($archiveEntries | Where-Object { $_ -eq "connection_topology.db" }).Count -gt 0) {
        throw "The archive unexpectedly contains connection_topology.db."
    }

    $archive = Get-Item -LiteralPath $archivePath
    $sizeMiB = [Math]::Round($archive.Length / 1MB, 2)
    Write-Host "Package created: $($archive.FullName)"
    Write-Host "File size: $sizeMiB MiB"
    Write-Host "Includes wheelhouse: $includesWheelhouse"
    Write-Warning "The archive contains .env. Transfer and store it securely."
} catch {
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    throw
}
