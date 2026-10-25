#!/bin/bash
# managed-by: qmd-auto-context
# qmd 데몬 생존 확인 핑.
#
# 이전 버전은 embedding context warm 유지를 위해 collection 없는 vec 쿼리를 주기적으로 보냈다.
# qmd 2.5.x의 /query vec 경로는 collection 지정이 없으면 전역 vectors_vec를 검색하므로,
# 큰 qmd index(수십만 vector)에서는 keepalive 한 번이 20~30초 이상 걸려 daemon health까지
# 막을 수 있다. 기본값은 health-only로 두고, 전역 vec warm ping은 명시 opt-in일 때만 실행한다.
#
# 주의: qmd 데몬은 single-threaded(Node 이벤트루프)다. 핑이 겹치거나 데몬이 busy인데 또 쏘면
# 서버를 더 바쁘게 만들어 /health 까지 막는다. 그래서:
#  (1) single-flight: 이전 핑이 아직 돌면 skip (mkdir atomic lock)
#  (2) health 가 즉답할 때만 선택적 vec warm 핑 (busy/down 이면 skip)
export PYTHONDONTWRITEBYTECODE=1
PORT="${QMD_DAEMON_PORT:-8584}"
URL="http://localhost:${PORT}"
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  PORT="$(python3 "$(dirname "$0")/../core/qmd_endpoint.py" port 2>/dev/null)" || exit 0
  URL="$(python3 "$(dirname "$0")/../core/qmd_endpoint.py" url 2>/dev/null)" || exit 0
fi
LOCKDIR="${TMPDIR:-/tmp}/qmd-keepalive.lock.d"

# (1) single-flight. 이미 도는 핑이 있으면(=lock 존재) 그냥 빠진다.
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # stale lock 방어: 10분 이상 오래된 lock 이면 정리하고 이번엔 skip.
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCKDIR" 2>/dev/null || true
  fi
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# (2) health 즉답 확인. busy/down 이면 핑을 추가로 쏘지 않는다.
curl --noproxy '*' -s -m 1 "${URL}/health" >/dev/null 2>&1 || exit 0

# 기본값은 health-only. 전역 vec warm ping은 큰 index에서 daemon을 장시간 점유할 수 있으므로
# 진단/실험 목적으로 명시 opt-in 한 경우에만 사용한다.
[ "${QMD_KEEPALIVE_VEC_WARM:-0}" = "1" ] || exit 0

# No attested server-side deadline/cancellation: optional warm safely defers.
# A client timeout cannot prove the daemon stopped its vector work.
python3 "$(dirname "$0")/../core/runtime_admission.py" warm >/dev/null 2>&1 || true
