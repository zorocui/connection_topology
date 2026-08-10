#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

if [ ! -f .env ]; then
    echo "missing .env; copy .env.example and fill in APP_SECRET_KEY and DATABASE_URL" >&2
    exit 1
fi

"$PY" -m alembic upgrade head

read -r WORKERS HOST PORT <<EOF
$("$PY" -c "from app.config import get_settings; from app.runtime import resolve_web_workers; s=get_settings(); print(resolve_web_workers(s.web_workers), s.host, s.port)")
EOF

"$PY" -m app.preflight --workers "$WORKERS"

exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --workers "$WORKERS"
