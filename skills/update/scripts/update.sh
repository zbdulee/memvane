#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
QMD_BACKEND_MANAGER="${QMD_BACKEND_MANAGER:-$PLUGIN_ROOT/core/backend_manager.sh}"
TARGET_CWD="${1:-$PWD}"
if [ -z "${QMD_SANDBOX:-}" ] && ! python3 "$PLUGIN_ROOT/core/setup_guard.py" check "$TARGET_CWD" >/dev/null 2>&1; then
  printf '[qmd] setup required: 이 프로젝트의 기존 데이터는 보존 중입니다. setup skill에 설치 계획 검토를 요청하세요.\n' >&2
  exit 1
fi
if [ "$#" -gt 0 ]; then shift; fi

if [ -z "${QMD_SANDBOX:-}" ]; then
  bash "$QMD_BACKEND_MANAGER" check-qmd --manual
  bash "$QMD_BACKEND_MANAGER" ensure --wait >/dev/null 2>&1 || true
  bash "$QMD_BACKEND_MANAGER" warm >/dev/null 2>&1 || true
  bash "$QMD_BACKEND_MANAGER" rotate >/dev/null 2>&1 || true
fi

cd "$TARGET_CWD"
export QMD_BACKEND_MANAGER
exec bash "$PLUGIN_ROOT/core/update.sh" "$@"
