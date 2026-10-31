#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
TARGET_CWD="${1:-$PWD}"
if [ -z "${QMD_SANDBOX:-}" ]; then
  # The writer publishes into the selected memvane-db index; it needs that route.
  _laya_state="$(python3 "$PLUGIN_ROOT/core/laya_index_host_route.py" classify compile "$TARGET_CWD" 2>/dev/null)" || _laya_state=blocked
  case "$_laya_state" in
    candidate) ;;
    legacy)
      echo '[memvane] 이 프로젝트는 MemVane 색인에 온보딩되지 않았습니다. setup skill로 온보딩하세요.' >&2
      exit 1
      ;;
    *) echo '[memvane] selected route unavailable; wiki writes refused' >&2; exit 3 ;;
  esac
fi

shift || true
EXTRA=()
for arg in "$@"; do
  case "$arg" in
    --budget-final) EXTRA+=("--budget-final") ;;  # the only pass-through flag
    *) echo "[memvane] unknown option: $arg" >&2; exit 2 ;;
  esac
done
python3 "$PLUGIN_ROOT/core/wiki_extract.py" --cwd "$TARGET_CWD" ${EXTRA[@]+"${EXTRA[@]}"}
