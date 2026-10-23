#!/bin/bash
# managed-by: qmd-auto-context
# one-shot 로그 크기 가드. 대상이 둘이고 **회전 방식이 서로 다르다**.
#
#  (1) 데몬 로그 — 데몬이 로그 fd 를 계속 열어 둔다. `: > log`(truncate)를 쓰면 fd offset 이
#      남아 sparse(앞부분 null) 파일이 되므로, mv 로 회전한 뒤 데몬을 graceful reload 해
#      새 프로세스가 새 빈 로그를 open 하게 한다.
#  (2) recall 진단 로그(`QMD_RECALL_LOG`) — 훅이 append 마다 open/close 하므로 붙잡힌 fd 가
#      **없다**. mv 만으로 끝이고 reload 는 하지 않는다: 데몬을 재시작할 이유가 없고,
#      진단 로그가 커졌다는 이유로 유료 경로(재색인·임베딩)를 흔들면 안 된다. 회전 순간에
#      쓰고 있던 프로세스는 옛 inode(= `.1`)에 그 줄을 쓰고 끝내므로 유실도 섞임도 없다.
#      이 로그는 프로젝트 무관 전역 파일 하나라 방치하면 계속 자란다(실측 14.6MB / 26,493줄).
#
# 크기 미달이면 아무것도 안 한다. 세대는 `.1` 하나만 유지하므로 총량은 대상당 ~2×MAX 로
# 유계다 — 계획서 §5 처럼 장기 수집을 할 때는 `.1` 이 덮이는 시점에 그 앞 세대가 사라진다.
set -u
export PYTHONDONTWRITEBYTECODE=1
HOME="${HOME:-/Users/$(/usr/bin/id -un)}"
PORT="${QMD_DAEMON_PORT:-8584}"
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  PORT="$(python3 "$(dirname "$0")/../core/qmd_endpoint.py" port 2>/dev/null)" || exit 0
fi
LOG="${QMD_DAEMON_LOG:-$HOME/.cache/qmd/mcp.daemon.log}"
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  LOG="$(python3 "$(dirname "$0")/../core/qmd_endpoint.py" log 2>/dev/null)" || exit 0
fi
MAX_BYTES=$((10 * 1024 * 1024))   # 10MB

# cross-platform 파일 크기: BSD(macOS) stat -f%z → GNU(Linux) stat -c%s → wc -c 폴백.
log_size() {
  local s
  s=$(stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || wc -c <"$1" 2>/dev/null || echo 0)
  s=$(printf '%s' "$s" | tr -dc '0-9')
  [ -z "$s" ] && s=0
  printf '%s' "$s"
}

over_max() {
  [ -f "$1" ] || return 1
  [ "$(log_size "$1")" -lt "$MAX_BYTES" ] && return 1
  return 0
}

# (2) 먼저 처리한다 — 데몬 로그 상태와 독립이어야 하기 때문이다. 데몬 로그가 작으면 아래에서
# `exit 0` 하므로 순서를 바꾸면 recall 로그 회전이 그 조건에 딸려 죽는다.
# `QMD_RECALL_LOG` 는 `hooks/run-hook` 이 export 한다(기본값 `$CACHE/qmd-<engine>-hook.log`)
# — 즉 훅 경로에서 부르면 항상 값이 있다. 수동 호출에서 비어 있으면 회전 대상이 없다.
RECALL_LOG="${QMD_RECALL_LOG:-}"
if [ -n "$RECALL_LOG" ] && over_max "$RECALL_LOG"; then
  mv -f "$RECALL_LOG" "$RECALL_LOG.1" 2>/dev/null || true
fi

# (1) 데몬 로그
over_max "$LOG" || exit 0
mv -f "$LOG" "$LOG.1" 2>/dev/null || exit 0
# 데몬을 SIGTERM 으로 graceful 재시작 → fd 닫히고 새 LOG 를 연다(sparse 문제 없음).
# (SIGKILL 강제종료는 SQLite clean close 를 막아 WAL checkpoint 누락 → WAL 누적. update.sh 와 동일 이슈)
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  # Only the endpoint coordinator may signal an attested plugin daemon.
  # A stale/raw PID file can belong to Slack or another unrelated service.
  python3 "$(dirname "$0")/../core/qmd_endpoint.py" reload >/dev/null 2>&1 || mv -f "$LOG.1" "$LOG" 2>/dev/null || true
elif [ -n "${QMD_BACKEND_MANAGER:-}" ] && [ -x "$QMD_BACKEND_MANAGER" ]; then
  "$QMD_BACKEND_MANAGER" reload >/dev/null 2>&1 || mv -f "$LOG.1" "$LOG" 2>/dev/null || true
elif [ -n "${QMD_DAEMON_PID:-}" ] && [ -f "$QMD_DAEMON_PID" ]; then
  pid="$(cat "$QMD_DAEMON_PID" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || mv -f "$LOG.1" "$LOG" 2>/dev/null || true
  fi
else
  # 재시작 실패 시 원복(데몬이 옛 inode=.1 에 계속 쓰므로 로그 연속성 유지)
  mv -f "$LOG.1" "$LOG" 2>/dev/null || true
fi
