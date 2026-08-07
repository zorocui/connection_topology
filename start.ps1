$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "未找到 .venv。请先运行：py -3.10 -m venv .venv"
}

if (-not (Test-Path ".env")) {
    throw "未找到 .env。请复制 .env.example，并生成 APP_SECRET_KEY。"
}

$python = ".\.venv\Scripts\python.exe"
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$workers = & $python -c "from app.config import get_settings; from app.runtime import resolve_web_workers; s=get_settings(); print(resolve_web_workers(s.web_workers))"
& $python -m app.preflight --workers $workers
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers $workers

