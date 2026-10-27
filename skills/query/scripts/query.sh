#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
QMD_BACKEND_MANAGER="${QMD_BACKEND_MANAGER:-$PLUGIN_ROOT/core/backend_manager.sh}"
TARGET_CWD="${1:-$PWD}"
QMD_GUARD_STATUS=0
if [ -z "${QMD_SANDBOX:-}" ]; then
  python3 "$PLUGIN_ROOT/core/setup_guard.py" check "$TARGET_CWD" >/dev/null 2>&1 || QMD_GUARD_STATUS=$?
fi
if [ "$QMD_GUARD_STATUS" -ne 0 ] && [ "$QMD_GUARD_STATUS" -ne 4 ]; then
  printf '[qmd] setup required: 이 프로젝트의 기존 데이터는 보존 중입니다. setup skill에 설치 계획 검토를 요청하세요.\n' >&2
  exit 1
fi

if [ "$#" -gt 0 ]; then shift; fi
QUERY_TEXT="$*"
if [ -z "$QUERY_TEXT" ]; then
  QUERY_TEXT="$(cat)"
fi

payload="$(python3 -c 'import json,sys; print(json.dumps({"hook_event_name":"UserPromptSubmit","prompt":sys.argv[2],"cwd":sys.argv[1]}, ensure_ascii=False))' "$TARGET_CWD" "$QUERY_TEXT")"
if [ "$QMD_GUARD_STATUS" -eq 4 ]; then
  # Linked git worktree of a selected main checkout: the Laya recall hook
  # serves that checkout's published index read-only. Never QMD.
  printf '%s' "$payload" | bash "$PLUGIN_ROOT/hooks/run-hook" recall claude
  exit $?
fi

if [ -z "${QMD_SANDBOX:-}" ]; then
  bash "$QMD_BACKEND_MANAGER" check-qmd --manual
  bash "$QMD_BACKEND_MANAGER" ensure --wait >/dev/null 2>&1 || true
fi

printf '%s' "$payload" | python3 "$PLUGIN_ROOT/core/recall.py"
