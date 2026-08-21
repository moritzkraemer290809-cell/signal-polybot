#!/usr/bin/env bash
# Container entrypoint: run migrations, then start the API process.
set -euo pipefail

echo "[entrypoint] applying database migrations"
alembic upgrade head

echo "[entrypoint] starting polysignal-intelligence API"
exec uvicorn app.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}"
