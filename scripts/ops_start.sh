#!/bin/sh
# No eval/sourced dotenv, optional extra workers, access logging, or credential echo.
set -eu
/app/.venv/bin/python /app/scripts/ops_config.py --local --runtime
exec /app/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8080 \
  --workers 1 --no-access-log --forwarded-allow-ips 127.0.0.1 \
  --timeout-graceful-shutdown 55
