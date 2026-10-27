#!/usr/bin/env bash
# 本地兜底运行：不依赖 GitHub Actions。
# 用法：bash scripts/local_run.sh            （真实续期）
#       bash scripts/local_run.sh --dry-run  （只看结果）
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${DIGITALPLAT_ENV_FILE:-$HOME/Library/Application Support/digitalplat-auto-renew/env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

: "${DIGITALPLAT_API_TOKEN:?请先设置 DIGITALPLAT_API_TOKEN，或写入 $ENV_FILE}"
export DIGITALPLAT_DOMAINS="${DIGITALPLAT_DOMAINS:-}"
export DIGITALPLAT_RENEW_BEFORE_DAYS="${DIGITALPLAT_RENEW_BEFORE_DAYS:-120}"

exec python3 scripts/digitalplat_auto_renew.py --state "${STATE_PATH:-state/domains-state.json}" "$@"
