#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
TARGET_CWD="${1:-$PWD}"
if [ "$#" -gt 0 ]; then shift; fi
QUERY_TEXT="$*"
if [ -z "$QUERY_TEXT" ]; then
  QUERY_TEXT="$(cat)"
fi
[ -n "${QMD_SANDBOX:-}" ] && exit 0

payload="$(python3 -c 'import json,sys; print(json.dumps({"hook_event_name":"UserPromptSubmit","prompt":sys.argv[2],"cwd":sys.argv[1]}, ensure_ascii=False))' "$TARGET_CWD" "$QUERY_TEXT")"

# Only a selected Laya route answers, from its own published index, through
# the same recall hook a host prompt uses.
_laya_state="$(python3 "$PLUGIN_ROOT/core/laya_index_host_route.py" classify recall "$TARGET_CWD" 2>/dev/null)" || _laya_state=blocked
case "$_laya_state" in
  candidate|preview|worktree)
    printf '%s' "$payload" | bash "$PLUGIN_ROOT/hooks/run-hook" recall claude
    exit $?
    ;;
  legacy)
    echo '[laya-index] 이 프로젝트는 Laya 색인에 온보딩되지 않았습니다. setup skill로 온보딩하세요.' >&2
    exit 1
    ;;
  *) echo '[laya-index] selected route unavailable; QMD fallback refused' >&2; exit 3 ;;
esac
