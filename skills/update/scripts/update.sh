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
    HOST_ENGINE="${2:-${MEMVANE_HOST:-${QMD_ENGINE:-}}}"
    if [ -z "$HOST_ENGINE" ]; then
      if [ -n "${CODEX_THREAD_ID:-}" ]; then HOST_ENGINE=codex
      elif [ -n "${CLAUDECODE:-}" ]; then HOST_ENGINE=claude
      elif [ -n "${HERMES_SESSION_ID:-}" ]; then HOST_ENGINE=hermes
      fi
    fi
    case "$HOST_ENGINE" in
      '') HOST_ENGINE=unknown ;;
      claude|codex|hermes|gemini|unknown) ;;
      *) echo '[memvane] current host is required; pass the host CLI name as the second argument' >&2; exit 2 ;;
    esac
    python3 -c 'import json,sys; print(json.dumps({"hook_event_name":"SessionStart","source":"startup","cwd":sys.argv[1]}))' "$TARGET_CWD" \
      | bash "$PLUGIN_ROOT/hooks/run-hook" update "$HOST_ENGINE"
    exit $?
    ;;
  legacy)
    echo '[memvane] 이 프로젝트는 MemVane 색인에 온보딩되지 않았습니다. setup skill로 온보딩하세요.' >&2
    exit 1
    ;;
  *) echo '[memvane] selected route unavailable; request refused' >&2; exit 3 ;;
esac
