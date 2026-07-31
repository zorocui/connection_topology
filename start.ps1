$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "未找到 .venv。请先运行：py -3.10 -m venv .venv"
}

if (-not (Test-Path ".env")) {
    throw "未找到 .env。请复制 .env.example，并生成 APP_SECRET_KEY。"
}

& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

