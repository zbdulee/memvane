#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
TARGET_CWD="${1:-$PWD}"
[ -n "${QMD_SANDBOX:-}" ] && exit 0

# A selected Laya route queues its own durable full-scan sync wake through the
# SessionStart hook. The wake reconciles created, changed and deleted files.
_laya_state="$(python3 "$PLUGIN_ROOT/core/laya_index_host_route.py" classify update "$TARGET_CWD" 2>/dev/null)" || _laya_state=blocked
case "$_laya_state" in
  candidate|preview|worktree)
    python3 -c 'import json,sys; print(json.dumps({"hook_event_name":"SessionStart","source":"startup","cwd":sys.argv[1]}))' "$TARGET_CWD" \
      | bash "$PLUGIN_ROOT/hooks/run-hook" update claude
    exit $?
    ;;
  legacy)
    echo '[memvane] 이 프로젝트는 MemVane 색인에 온보딩되지 않았습니다. setup skill로 온보딩하세요.' >&2
    exit 1
    ;;
  *) echo '[memvane] selected route unavailable; QMD fallback refused' >&2; exit 3 ;;
esac
