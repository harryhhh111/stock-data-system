#!/usr/bin/env bash
# Daily backup entry point. Keep cron compatible; policy lives in the stdlib helper.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
exec "${STOCK_BACKUP_PYTHON:-python3}" "$PROJECT_ROOT/scripts/backup_db_to_cos.py" "$@"
