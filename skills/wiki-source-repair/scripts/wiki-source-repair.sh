#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

# 사용법:
#   wiki-source-repair.sh <cwd> list [limit]                      # limit = 소실 파일 수
#   wiki-source-repair.sh <cwd> repoint-source <oldSourcePath> <newSourcePath>
#   wiki-source-repair.sh <cwd> dismiss-source <sourcePath>
#   wiki-source-repair.sh <cwd> repoint <cardPath> <oldSourcePath> <newSourcePath>
#   wiki-source-repair.sh <cwd> dismiss <cardPath>
#
# `-source` 동사가 기본이다: 결정 단위는 카드가 아니라 **소실 파일**이다(실측 대기 카드
# 73장 ← 서로 다른 소실 파일 13개). 카드 단위 동사는 한 그룹 안에서 카드마다 판단이
# 갈릴 때만 쓴다.
#
# 데몬/인덱스를 쓰지 않는 파일시스템 작업이므로 backend manager는 호출하지 않는다.

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
USAGE="usage: wiki-source-repair.sh <cwd> <list|repoint-source|dismiss-source|repoint|dismiss> [...]"
TARGET_CWD="${1:?$USAGE}"
if [ -z "${QMD_SANDBOX:-}" ] && ! python3 "$PLUGIN_ROOT/core/setup_guard.py" check "$TARGET_CWD" >/dev/null 2>&1; then
  printf '[qmd] setup required: 이 프로젝트의 기존 데이터는 보존 중입니다. setup skill에 설치 계획 검토를 요청하세요.\n' >&2
  exit 1
fi
ACTION="${2:?$USAGE}"

case "$ACTION" in
  list)
    # limit(선택, 0=전체)은 **소실 파일 수**다(카드 수가 아니다 — 한 파일을 카드 수십
    # 장이 인용한다). 그룹 정렬은 trusted 포함 먼저 → 카드 수 많은 순 → 오래된 감지
    # 순이라 배치가 재현된다.
    python3 "$PLUGIN_ROOT/core/wiki_source_repair.py" --cwd "$TARGET_CWD" --list \
      --limit "${3:-0}"
    ;;
  repoint)
    CARD="${3:?usage: wiki-source-repair.sh <cwd> repoint <cardPath> <oldSourcePath> <newSourcePath>}"
    OLD="${4:?usage: wiki-source-repair.sh <cwd> repoint <cardPath> <oldSourcePath> <newSourcePath>}"
    NEW="${5:?usage: wiki-source-repair.sh <cwd> repoint <cardPath> <oldSourcePath> <newSourcePath>}"
    python3 "$PLUGIN_ROOT/core/wiki_source_repair.py" --cwd "$TARGET_CWD" \
      --repoint "$CARD" --from "$OLD" --to "$NEW"
    ;;
  dismiss)
    CARD="${3:?usage: wiki-source-repair.sh <cwd> dismiss <cardPath>}"
    python3 "$PLUGIN_ROOT/core/wiki_source_repair.py" --cwd "$TARGET_CWD" --dismiss "$CARD"
    ;;
  repoint-source)
    # 그 파일을 인용하는 **대기 카드 전부**에 한 번에 적용한다. 자동 적용이 아니다 —
    # 잘못 매칭한 일괄 재지정은 카드 수십 장을 한 번에 verify-삭제 코스로 보낸다.
    OLD="${3:?usage: wiki-source-repair.sh <cwd> repoint-source <oldSourcePath> <newSourcePath>}"
    NEW="${4:?usage: wiki-source-repair.sh <cwd> repoint-source <oldSourcePath> <newSourcePath>}"
    python3 "$PLUGIN_ROOT/core/wiki_source_repair.py" --cwd "$TARGET_CWD" \
      --repoint-source "$OLD" --to "$NEW"
    ;;
  dismiss-source)
    SRC="${3:?usage: wiki-source-repair.sh <cwd> dismiss-source <sourcePath>}"
    python3 "$PLUGIN_ROOT/core/wiki_source_repair.py" --cwd "$TARGET_CWD" --dismiss-source "$SRC"
    ;;
  *)
    echo "unknown action: $ACTION ($USAGE)" >&2
    exit 2
    ;;
esac
