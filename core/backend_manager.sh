#!/usr/bin/env bash
set -u

# 샌드박스 가드는 여기가 마지막 방어선이다. 현재 호출부 4갈래(hooks/run-hook,
# skills/*/scripts/*.sh, backend/index_worker.sh, hermes_adapter/core_bridge.py)가
# 각자 같은 검사를 하지만 가드가 호출부에 흩어져 있으면 다음에 추가되는 경로가
# 빠뜨린다(Python 훅의 per-site try/except가 정확히 그렇게 반복 실패해 hook_main.py
# 한 곳으로 모았다). QMD_SANDBOX를 켜는 곳은 core/extractors/lib.py 하나이고 —
# wiki 카드를 만들 때 띄우는 host CLI 자식이 자기 qmd 훅을 또 발동시키는 재귀를
# 막는다 — 그 자식이 여기에 도달하면 reload가 실제로 데몬을 죽인다(이 커밋 전에는
# reload가 no-op이라 대가가 없었다).
[ -n "${QMD_SANDBOX:-}" ] && exit 0
[ -n "${GEMINI_SANDBOX:-}" ] && exit 0

# Backend Python imports must not mutate the exact installed plugin tree.
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)" || exit 0
PORT="${QMD_DAEMON_PORT:-8584}"
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  STATE_DIR="${QMD_BACKEND_STATE_DIR:-$HOME/Library/Application Support/qmd-auto-context/backend}"
  selected_port="$(python3 "$ROOT/core/qmd_endpoint.py" port 2>/dev/null)" || selected_port=""
  [ -z "$selected_port" ] || PORT="$selected_port"
else
  STATE_DIR="${QMD_BACKEND_STATE_DIR:-${TMPDIR:-/tmp}/qmd-auto-context-backend}"
fi
# A second QMD instance (for example the user's :8483 service) can share the
# default state directory. Keep each port's managed PID proof separate.
PID_FILE="${QMD_DAEMON_PID:-$STATE_DIR/daemon-${PORT}.pid}"
LEGACY_PID_FILE=""
[ -z "${QMD_DAEMON_PID:-}" ] && LEGACY_PID_FILE="$STATE_DIR/daemon.pid"
MANAGER_LOG="${QMD_BACKEND_LOG:-$HOME/.cache/qmd/backend-manager.log}"
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  DAEMON_LOG="$STATE_DIR/daemon.log"
else
  DAEMON_LOG="${QMD_DAEMON_LOG:-$HOME/.cache/qmd/mcp.daemon.log}"
fi
DAEMON_SCRIPT="${QMD_DAEMON_SCRIPT:-$ROOT/backend/daemon.sh}"
KEEPALIVE_SCRIPT="${QMD_KEEPALIVE_SCRIPT:-$ROOT/backend/keepalive.sh}"
LOGROTATE_SCRIPT="${QMD_LOGROTATE_SCRIPT:-$ROOT/backend/logrotate.sh}"
INDEX_WORKER_SCRIPT="${QMD_INDEX_WORKER_SCRIPT:-$ROOT/backend/index_worker.sh}"
COMPILE_WORKER_SCRIPT="${QMD_COMPILE_WORKER_SCRIPT:-$ROOT/core/wiki_compile_worker.py}"
KICK_LOCK="${QMD_WORKER_KICK_LOCKDIR:-$STATE_DIR/index-kick.lock.d}"
COMPILE_KICK_LOCK="${QMD_COMPILE_WORKER_KICK_LOCKDIR:-$STATE_DIR/wiki-compile-kick.lock.d}"
COMPILE_RETRY_LOCK="${QMD_COMPILE_RETRY_LOCKDIR:-$STATE_DIR/wiki-compile-retry.lock.d}"
START_LOCK="${QMD_DAEMON_START_LOCKDIR:-$STATE_DIR/daemon-start.lock.d}"
RELOAD_LOCK="${QMD_DAEMON_RELOAD_LOCKDIR:-$STATE_DIR/daemon-reload.lock.d}"
REQUIRED_QMD_VERSION="${QMD_REQUIRED_VERSION:-2.5.3}"
SUPPORTED_QMD_MAJOR="${QMD_SUPPORTED_MAJOR:-2}"

. "$ROOT/core/qmd_path.sh"

( umask 077; mkdir -p "$STATE_DIR" "$(dirname "$MANAGER_LOG")" "$(dirname "$DAEMON_LOG")" ) 2>/dev/null || true

log() {
  printf '[%s] backend-manager: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$MANAGER_LOG" 2>&1 || true
}

qmd_health_timeout() {
  python3 - <<'PY'
import math
import os

default = 2.0
try:
    value = float(os.environ.get("QMD_HEALTH_TIMEOUT", default))
except (TypeError, ValueError):
    value = default
if not math.isfinite(value) or value <= 0:
    value = default
print(f"{value:g}")
PY
}

health() {
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    python3 "$ROOT/core/qmd_endpoint.py" health >/dev/null 2>&1
    return $?
  fi
  local timeout
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    python3 "$ROOT/core/qmd_route.py" resolve-shared-env >/dev/null 2>&1 || return 1
    pid_is_daemon "$(read_pid)" || return 1
  fi
  timeout="$(qmd_health_timeout)"
  # localhost(not 127.0.0.1): qmd 데몬은 IPv6 ::1에만 바인딩된다. 127.0.0.1(IPv4)로
  # 찌르면 refused → false-dead 판정 → 데몬 재시작 crash loop. curl localhost는
  # ::1/127.0.0.1 둘 다 시도하므로 바인딩 계열과 무관하게 안전(recall.py·keepalive.sh와 일치).
  curl -sf -m "$timeout" "http://localhost:${PORT}/health" >/dev/null 2>&1
}

qmd_version() {
  local qmd_bin
  qmd_bin="$(resolve_qmd_bin 2>/dev/null)" || return 1
  "$qmd_bin" --version 2>/dev/null | sed -E 's/^qmd[[:space:]]+//'
}

version_ok() {
  local version="$1"
  python3 - "$version" "$REQUIRED_QMD_VERSION" "$SUPPORTED_QMD_MAJOR" <<'PY'
import re
import sys

version, required, major = sys.argv[1:4]

def parse(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", v.strip())
    if not m:
        raise ValueError(v)
    return tuple(int(x) for x in m.groups())

try:
    current = parse(version)
    minimum = parse(required)
    supported_major = int(major)
except ValueError:
    sys.exit(1)

if current[0] != supported_major:
    sys.exit(1)
sys.exit(0 if current >= minimum else 1)
PY
}

install_hint() {
  printf 'qmd is not installed or is too old. Install a tested qmd version:\n'
  printf '  bun add -g @tobilu/qmd@%s\n' "$REQUIRED_QMD_VERSION"
  printf '  # or: npm install -g @tobilu/qmd@%s\n' "$REQUIRED_QMD_VERSION"
}

check_qmd() {
  local mode="${1:-}"
  local version
  version="$(qmd_version || true)"
  if [ -z "$version" ] || ! version_ok "$version"; then
    log "qmd dependency missing_or_unsupported version=${version:-missing} required=$REQUIRED_QMD_VERSION major=$SUPPORTED_QMD_MAJOR"
    [ "$mode" = "--manual" ] && install_hint
    return 1
  fi
  return 0
}

pid_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

pid_command() {
  local pid="$1"
  ps -p "$pid" -o command= 2>/dev/null || true
}

pid_is_daemon() {
  local pid="$1"
  local cmd
  pid_alive "$pid" || return 1
  cmd="$(pid_command "$pid")"
  printf '%s' "$cmd" | grep -q "mcp --http" || return 1
  # 포트는 단어 경계까지 봐야 한다 — grep "--port $PORT"는 접두 매칭이라
  # PORT=1이 `--port 1234` 데몬을 자기 것으로 오판하고 남의 프로세스에 TERM을 보낸다.
  # case 패턴이라 PORT에 정규식 메타문자가 들어와도 안전하다.
  case " $cmd " in
    *" --port $PORT "*|*" --port=$PORT "*) ;;
    *) return 1 ;;
  esac
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    local spec entry
    spec="$(python3 "$ROOT/core/qmd_route.py" daemon-spec 2>/dev/null)" || return 1
    entry="${spec%%$'\x1f'*}"
    [ -n "$entry" ] || return 1
    case " $cmd " in
      *" $entry mcp --http "*) ;;
      *) return 1 ;;
    esac
  fi
  return 0
}

pid_is_starting_daemon() {
  local pid="$1"
  local cmd
  pid_alive "$pid" || return 1
  cmd="$(pid_command "$pid")"
  printf '%s' "$cmd" | grep -q "bash $DAEMON_SCRIPT" && return 0
  printf '%s' "$cmd" | grep -q "backend/daemon.sh" && return 0
  return 1
}

read_pid() {
  cat "$PID_FILE" 2>/dev/null || true
}

# On upgrade, carry an old default proof forward only when its live process
# passes the exact executable and port checks. A legacy file for :8483 cannot
# authorize the plugin's selected-endpoint daemon. Explicit QMD_DAEMON_PID never migrates.
migrate_legacy_pid() {
  [ -n "$LEGACY_PID_FILE" ] && [ ! -e "$PID_FILE" ] && [ ! -L "$PID_FILE" ] || return 0
  local pid
  pid="$(cat "$LEGACY_PID_FILE" 2>/dev/null || true)"
  pid_is_daemon "$pid" || return 0
  ( umask 077; printf '%s\n' "$pid" >"$PID_FILE" ) || return 1
  log "daemon PID proof migrated pid=$pid port=$PORT"
}

# 포트를 LISTEN 중인 데몬 pid를 찾는다(발견은 항상 pid_is_daemon 재검증을 거친다).
# lsof 우선 — 기본적으로 자기 소유 프로세스만 보이고 "이 포트를 실제로 쥔 자"를 답한다.
# pgrep은 폴백이며 uid를 제한하고 cmdline에 qmd 진입점이 있는지까지 본다("mcp --http"만으로는
# 에이전트 CLI argv(예: 이 문자열을 인자로 든 명령)에 오탐한다). 둘 다 없으면 조용히 빈 문자열.
discover_daemon_pid() {
  local pid
  if command -v lsof >/dev/null 2>&1; then
    # 한 포트의 LISTEN 소유자는 사실상 1건이라 첫 줄만 본다. (개행이 kill로 흘러가는 것은
    # for의 단어분리가 이미 막는다 — 아래 pgrep 분기가 그래서 head 없이 전수 검증한다.)
    for pid in $(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -n1); do
      case "$pid" in ''|*[!0-9]*) continue ;; esac
      if pid_is_daemon "$pid"; then
        printf '%s' "$pid"
        return 0
      fi
    done
  fi
  if command -v pgrep >/dev/null 2>&1; then
    # 여기서는 head -n1로 자르지 않는다 — 다른 포트의 qmd 데몬(사용자의 :8483)이 먼저
    # 나오는 것이 정상이라 첫 줄만 보면 우리 포트의 데몬을 놓친다. 후보를 전부 검증하고
    # 통과한 **하나만** 반환하므로 개행 포함 값이 kill로 흘러갈 여지는 없다.
    for pid in $(pgrep -u "$(id -u)" -f "mcp --http" 2>/dev/null); do
      case "$pid" in ''|*[!0-9]*) continue ;; esac
      pid_is_daemon "$pid" || continue
      case "$(pid_command "$pid")" in
        *dist/cli/qmd.js*|*qmd*) printf '%s' "$pid"; return 0 ;;
      esac
    done
  fi
  return 0
}

# 데몬 pid 조회의 단일 진입점. **핵심은 죽이기가 아니라 추적 복구(입양)다.**
# PID_FILE은 start_daemon이 실제로 스폰할 때만 쓰이는데 스폰은 health 실패 시에만 일어나므로,
# 데몬이 살아 있는 한 파일이 한 번 지워지면 영원히 복구되지 않았다. 그 상태에서 reload는
# 로그 한 줄 없는 no-op이 되고(추적 유실), start_daemon은 살아 있는 포트에 재스폰해 EADDRINUSE를 냈다.
daemon_pid() {
  local pid
  pid="$(read_pid)"
  if pid_is_daemon "$pid"; then
    printf '%s' "$pid"
    return 0
  fi
  # In the shared-store runtime a port occupant without our PID proof is
  # foreign. Never adopt or signal the user's separately installed QMD.
  [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ] && return 0
  pid="$(discover_daemon_pid)"
  if [ -n "$pid" ]; then
    echo "$pid" >"$PID_FILE" 2>/dev/null || true
    log "daemon adopted pid=$pid port=$PORT"
    printf '%s' "$pid"
  fi
  return 0
}

# Occupancy alone never proves ownership. It does prove that replacing the
# current PID file with a newly spawned process would destroy the only managed
# PID evidence while that process cannot bind this port. This check is only a
# veto; it never adopts or signals the listener.
port_occupied() {
  if command -v lsof >/dev/null 2>&1 &&
     [ -n "$(lsof -nP -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null)" ]; then
    return 0
  fi
  python3 - "$PORT" <<'PY'
import socket
import sys

try:
    port = int(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if not 1 <= port <= 65535:
    raise SystemExit(1)
for family, address in ((socket.AF_INET6, ('::1', port)),
                        (socket.AF_INET, ('127.0.0.1', port))):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(address) == 0:
                raise SystemExit(0)
    except OSError:
        pass
raise SystemExit(1)
PY
}

start_daemon() {
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    python3 "$ROOT/core/qmd_route.py" resolve-shared-env >/dev/null 2>&1 || return 0
    check_qmd >/dev/null 2>&1 || return 0
    python3 "$ROOT/core/qmd_endpoint.py" ensure >/dev/null
    return $?
  fi
  migrate_legacy_pid || return 1
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    python3 "$ROOT/core/qmd_route.py" resolve-shared-env >/dev/null 2>&1 || return 0
  fi
  check_qmd >/dev/null 2>&1 || return 0
  if health; then
    # 살아 있는데 추적이 끊긴 경우에만 발견 비용(lsof/pgrep)을 낸다. 입양이 성공하면
    # PID_FILE이 유효해져 이후 호출은 ps 한 번으로 끝난다(훅마다 lsof를 돌리지 않는다).
    pid_is_daemon "$(read_pid)" || daemon_pid >/dev/null
    return 0
  fi
  if ! mkdir "$START_LOCK" 2>/dev/null; then
    if [ -n "$(find "$START_LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
      rmdir "$START_LOCK" 2>/dev/null || true
      mkdir "$START_LOCK" 2>/dev/null || { wait_health || true; return 0; }
    else
      wait_health || true
      return 0
    fi
  fi
  local pid
  if pid_is_starting_daemon "$(read_pid)"; then
    rmdir "$START_LOCK" 2>/dev/null || true
    return 0
  fi
  # daemon_pid는 포트로 살아 있는 데몬을 발견하면 PID_FILE에 다시 써 넣는다(입양).
  # 이 입양이 없으면 pid 파일이 비었을 때 살아 있는 포트에 재스폰해 EADDRINUSE가 반복된다.
  pid="$(daemon_pid)"
  if [ -n "$pid" ]; then
    rmdir "$START_LOCK" 2>/dev/null || true
    return 0
  fi
  if port_occupied; then
    log "daemon start skipped: port occupied without managed PID proof port=$PORT"
    rmdir "$START_LOCK" 2>/dev/null || true
    return 1
  fi
  rm -f "$PID_FILE" 2>/dev/null || true
  QMD_DAEMON_PORT="$PORT" nohup bash "$DAEMON_SCRIPT" >>"$DAEMON_LOG" 2>&1 &
  echo "$!" >"$PID_FILE" 2>/dev/null || true
  log "daemon start pid=$!"
  rmdir "$START_LOCK" 2>/dev/null || true
}

wait_health() {
  local max="${QMD_DAEMON_READY_ATTEMPTS:-60}"
  local i=0
  while [ "$i" -lt "$max" ]; do
    health && return 0
    i=$((i + 1))
    sleep 0.5
  done
  log "daemon health wait timeout port=$PORT"
  return 1
}

cleanup_legacy() {
  local launch_agents="$HOME/Library/LaunchAgents"
  local qmd_config="$HOME/.config/qmd"
  local label plist script path
  for label in com.qmd-mcp-daemon com.qmd-keepalive com.qmd-logrotate com.qmd-index-worker; do
    plist="$launch_agents/$label.plist"
    if has_marker "$plist"; then
      if command -v launchctl >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || launchctl unload "$plist" >/dev/null 2>&1 || true
      fi
      rm -f "$plist" 2>/dev/null || true
      log "removed legacy LaunchAgent $label"
    fi
  done
  for script in daemon.sh keepalive.sh logrotate.sh index_worker.sh; do
    path="$qmd_config/$script"
    if has_marker "$path"; then
      rm -f "$path" 2>/dev/null || true
      log "removed legacy script $path"
    fi
  done
}

ensure() {
  [ "${QMD_CLEANUP_LEGACY:-}" = "1" ] && cleanup_legacy >/dev/null 2>&1 || true
  check_qmd >/dev/null 2>&1 || return 0
  start_daemon
  if [ "${1:-}" = "--wait" ]; then
    wait_health || true
  fi
}

warm() {
  QMD_DAEMON_PORT="$PORT" bash "$KEEPALIVE_SCRIPT" >/dev/null 2>&1 || true
}

rotate() {
  # QMD_BACKEND_MANAGER를 넘기지 않던 동안 logrotate.sh의 첫 branch(manager reload)가
  # **구조적으로 도달 불가**였다 — 호출부 3곳(run-hook·update skill·hermes bridge)이 전부
  # export 타이밍/상속에 의존해 비어 있었다. 여기서 직접 넘기는 것이 SSOT다.
  QMD_DAEMON_PORT="$PORT" QMD_BACKEND_MANAGER="${QMD_BACKEND_MANAGER:-$ROOT/core/backend_manager.sh}" \
    QMD_DAEMON_PID="$PID_FILE" QMD_DAEMON_LOG="$DAEMON_LOG" bash "$LOGROTATE_SCRIPT" >/dev/null 2>&1 || true
}

wait_pid_exit() {
  local pid="$1"
  local max="${QMD_DAEMON_SHUTDOWN_ATTEMPTS:-60}"
  local i=0
  while [ "$i" -lt "$max" ]; do
    pid_is_daemon "$pid" || return 0
    i=$((i + 1))
    sleep 0.5
  done
  log "daemon graceful shutdown timeout pid=$pid"
  return 1
}

# PID_FILE이 지금도 이 pid를 가리킬 때만 지운다. 무조건 rm하면 그 사이 다른 경로가
# 써 넣은 정상 pid까지 날려 추적을 잃는다(그 상태의 reload는 조용한 no-op이 된다).
clear_pid_file_if() {
  [ "$(read_pid)" = "$1" ] && rm -f "$PID_FILE" 2>/dev/null
  return 0
}

reload_locked() {
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    python3 "$ROOT/core/qmd_endpoint.py" reload >/dev/null
    return $?
  fi
  local pid file_pid
  migrate_legacy_pid || return 1
  file_pid="$(read_pid)"
  pid="$(daemon_pid)"
  if [ -n "$pid" ]; then
    if kill -TERM "$pid" >/dev/null 2>&1; then
      log "daemon SIGTERM pid=$pid"
      # 종료를 못 봤으면 재시작하지 않는다. 옛 프로세스가 포트를 쥔 채라 재스폰은
      # EADDRINUSE로 죽고 죽은 pid가 PID_FILE에 남는다(이 커밋이 고친 바로 그 상태).
      wait_pid_exit "$pid" || return 1
      clear_pid_file_if "$pid"
    else
      # EPERM/ESRCH: 신호가 가지 않았으므로 프로세스는 그대로다. 여기서 wait_pid_exit에
      # 들어가면 기본 60×0.5s = 30초를 확실히 실패할 대기에 태운다(훅 블로킹).
      log "daemon SIGTERM failed pid=$pid — skip shutdown wait"
      return 1
    fi
  elif port_occupied; then
    log "daemon reload skipped: port occupied without managed PID proof port=$PORT"
    return 1
  elif [ -n "$file_pid" ]; then
    log "ignore stale/non-qmd daemon pid=$file_pid"
    clear_pid_file_if "$file_pid"
  fi
  start_daemon
  # 반환값 = "데몬을 실제로 재시작했는가". 호출자가 이걸로 되돌린다:
  # logrotate.sh:25는 reload 실패 시 `mv $LOG.1 $LOG`로 회전을 원복한다(데몬이 옛 inode에
  # 계속 쓰므로 이름을 되돌리면 로그 연속성이 유지된다). 항상 0을 돌려주던 동안 그 원복은
  # 도달 불가 죽은 코드였고, 실패하면 $LOG가 없는 채로 남아 이후 회전이 전부
  # logrotate.sh:14에서 조기 종료 → .1 무한 증가였다.
  wait_health
}

reload() {
  # 직렬화: 락이 없던 동안 동시 reload 2건이 서로의 데몬을 죽이고 패자가 EADDRINUSE로
  # 즉사했다(실측 14:06 로그). stale 회수는 저장소 관례인 find -mmin +10(=LOCK_STALE_SECS 600).
  if ! mkdir "$RELOAD_LOCK" 2>/dev/null; then
    if [ -n "$(find "$RELOAD_LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
      rmdir "$RELOAD_LOCK" 2>/dev/null || true
      mkdir "$RELOAD_LOCK" 2>/dev/null || { log "reload skipped: lock busy"; return 1; }
    else
      # 승자가 재시작 중일 수 있지만 "내가 재시작했다"고 말하지 않는다 — 호출자의
      # 보수적 원복(로그 이름 되돌리기)은 무해하고 다음 회차가 다시 시도한다.
      log "reload skipped: lock busy"
      return 1
    fi
  fi
  # trap: 하드킬(Hermes core_bridge.py는 rotate를 8s에 끊는다)에도 락이 남지 않게 한다.
  # 남으면 stale 회수 10분 동안 모든 reload가 skip된다. kick_index의 선례와 같은 규칙.
  trap 'rmdir "$RELOAD_LOCK" 2>/dev/null || true' EXIT INT TERM
  reload_locked
  local rc=$?
  rmdir "$RELOAD_LOCK" 2>/dev/null || true
  trap - EXIT INT TERM
  return "$rc"
}

kick_index() {
  if ! mkdir "$KICK_LOCK" 2>/dev/null; then
    if python3 "$ROOT/core/worker_lock.py" reclaim "$KICK_LOCK" >>"$MANAGER_LOG" 2>&1; then
      mkdir "$KICK_LOCK" 2>/dev/null || return 0
    else
      # busy: 실행 중인 worker에게 재-drain 요청(lost-wakeup 방지). worker가 큐를
      # 스냅샷한 뒤 enqueue된 항목이 다음 루프에서 처리된다. 이걸 안 하면 KICK_LOCK을
      # 쥔 긴 embed(수 분) 동안 들어온 compile/verify 카드가 다음 SessionStart까지 대기.
      : >"$KICK_LOCK/rekick" 2>/dev/null || true
      return 0
    fi
  fi
  exec 8<"$KICK_LOCK" 9>"$KICK_LOCK/pid" || return 0
  (
    # trap이 rekick도 지워야 rmdir(빈 디렉토리 요구)이 성공한다 — 안 지우면 lock 누수.
    trap 'python3 "$ROOT/core/worker_lock.py" release-rekick "$KICK_LOCK" 9 8 >>"$MANAGER_LOG" 2>&1 || true' EXIT
    # Bash $$ names the parent even in a background subshell. The parent
    # publishes $! below; construction without a PID is conservatively busy.
    for publication_try in {1..50}; do
      python3 "$ROOT/core/worker_lock.py" published "$KICK_LOCK" 9 8 >/dev/null 2>&1 && break
      sleep .02
    done
    python3 "$ROOT/core/worker_lock.py" published "$KICK_LOCK" 9 8 >/dev/null 2>&1 || exit 0
    while :; do
      # 이번 run이 커버할 것으로 간주하고 요청을 먼저 소비. run "도중" 들어온 rekick만
      # 남아 다음 루프를 돈다(잔여 window = 마지막 체크~lock 해제 사이 마이크로초, self-heal).
      rm -f "$KICK_LOCK/rekick" 2>/dev/null || true
      QMD_DAEMON_PORT="$PORT" QMD_BACKEND_MANAGER="$ROOT/core/backend_manager.sh" bash "$INDEX_WORKER_SCRIPT" >>"$MANAGER_LOG" 2>&1 || true
      [ -e "$KICK_LOCK/rekick" ] || break
    done
  ) >/dev/null 2>&1 &
  echo "$!" >&9
  exec 8<&- 9>&-
}

compile_wake_seconds() {
  python3 - "$1" <<'PY' 2>/dev/null || true
import json
import math
import sys

try:
    payload = json.loads(sys.argv[1])
    value = payload.get("wakeAfterSeconds") if isinstance(payload, dict) else None
    seconds = int(math.ceil(float(value)))
    if seconds > 0:
        # A malformed/custom worker must not leave an unbounded sleeping process.
        print(min(seconds, 86400))
except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
    pass
PY
}

schedule_wiki_compile_retry() {
  local cwd="$1"
  local lock_hash="$2"
  local delay="$3"
  local retry_lock="${COMPILE_RETRY_LOCK}.${lock_hash}"
  [ -z "$delay" ] && return 0
  if ! mkdir "$retry_lock" 2>/dev/null; then
    python3 "$ROOT/core/worker_lock.py" reclaim "$retry_lock" >>"$MANAGER_LOG" 2>&1 || return 0
    mkdir "$retry_lock" 2>/dev/null || return 0
  fi
  exec 4<"$retry_lock" 5>"$retry_lock/pid" || return 0
  (
    trap 'python3 "$ROOT/core/worker_lock.py" release "$retry_lock" 5 4 >>"$MANAGER_LOG" 2>&1 || true' EXIT
    sleep "$delay" || exit 0
    # Release before re-kicking: otherwise a newly deferred worker would see
    # this predecessor as live and lose its next wake-up.
    python3 "$ROOT/core/worker_lock.py" release "$retry_lock" 5 4 >>"$MANAGER_LOG" 2>&1 || exit 0
    trap - EXIT
    kick_wiki_compile "$cwd"
  ) >/dev/null 2>&1 &
  # Bash 3.2's `$$` remains the parent shell in a subshell. `$!` is the
  # portable PID of the background sleeper whose liveness owns this lock.
  echo "$!" >&5
  exec 4<&- 5>&-
}

kick_wiki_compile() {
  [ -n "${QMD_AUTO_COMPILE_DISABLED:-}" ] && return 0
  local cwd="${1:-}"
  local flush="${2:-}"
  local flush_arg=""
  [ "$flush" = "--flush" ] && flush_arg="--flush-all"
  local lock_hash lock_dir worker_report wake_seconds
  [ -z "$cwd" ] && return 0
  lock_hash="$(python3 - "$cwd" <<'PY' 2>/dev/null || true
import hashlib
import sys
print(hashlib.sha256(sys.argv[1].encode('utf-8')).hexdigest()[:16])
PY
)"
  [ -z "$lock_hash" ] && lock_hash="default"
  lock_dir="${COMPILE_KICK_LOCK}.${lock_hash}"
  if ! mkdir "$lock_dir" 2>/dev/null; then
    if python3 "$ROOT/core/worker_lock.py" reclaim "$lock_dir" >>"$MANAGER_LOG" 2>&1; then
      mkdir "$lock_dir" 2>/dev/null || return 0
    else
      return 0
    fi
  fi
  exec 6<"$lock_dir" 7>"$lock_dir/pid" || return 0
  (
    trap 'python3 "$ROOT/core/worker_lock.py" release "$lock_dir" 7 6 >>"$MANAGER_LOG" 2>&1 || true' EXIT
    for publication_try in {1..50}; do
      python3 "$ROOT/core/worker_lock.py" published "$lock_dir" 7 6 >/dev/null 2>&1 && break
      sleep .02
    done
    python3 "$ROOT/core/worker_lock.py" published "$lock_dir" 7 6 >/dev/null 2>&1 || exit 0
    case "$COMPILE_WORKER_SCRIPT" in
      *.sh|*.bash) bash "$COMPILE_WORKER_SCRIPT" --cwd "$cwd" $flush_arg >>"$MANAGER_LOG" 2>&1 || true ;;
      *) worker_report="$(python3 "$COMPILE_WORKER_SCRIPT" --cwd "$cwd" $flush_arg --json 2>>"$MANAGER_LOG" || true)" ;;
    esac
    wake_seconds="$(compile_wake_seconds "$worker_report")"
    [ -n "$wake_seconds" ] && schedule_wiki_compile_retry "$cwd" "$lock_hash" "$wake_seconds"
    # compile worker(+피기백 verify)가 dirty 큐에 enqueue한 wiki collection을 즉시 drain해
    # 다음 SessionStart 전에 같은 세션에서 recall-visible하게 만든다. 편집 자신의 index
    # kick은 배치/verify 지연 때문에 카드가 큐에 오르기 전에 이미 drain을 마쳐 놓친다.
    # index_worker는 빈 큐에 no-op이고 KICK/WRITER 락으로 single-flight라 double-kick 무해.
    # 알려진 bound: 이 시점 다른 kick이 KICK_LOCK을 쥐고 있으면 여기 kick은 busy로 drop된다.
    # 그래도 큐는 보존되므로 다음 kick/SessionStart에 drain된다(기존엔 항상 SessionStart까지 대기).
    kick_index
  ) >/dev/null 2>&1 &
  echo "$!" >&7
  exec 6<&- 7>&-
}

has_marker() {
  [ -f "$1" ] && grep -q "managed-by: qmd-auto-context" "$1" 2>/dev/null
}

daemon_identity() {
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    python3 "$ROOT/core/qmd_endpoint.py" identity
    return $?
  fi
  local pid
  # Identity is an inspection command: discover a managed process without
  # adopting it into PID_FILE or changing backend state.
  pid="$(read_pid)"
  if ! pid_is_daemon "$pid"; then
    if [ "${QMD_SETUP_GUARD_FIXTURE:-}" = 1 ]; then
      pid="$(discover_daemon_pid)"
    else
      pid=""
    fi
  fi
  if [ -z "$pid" ]; then
    health && return 2  # A healthy service without a managed PID is not "absent".
    return 1
  fi
  printf '%s\t%s\n' "$pid" "$(pid_command "$pid")"
  health && return 0
  return 3  # Managed PID exists, but its health endpoint failed.
}

case "${1:-}" in
  identity) daemon_identity ;;
  health) health || true ;;
  health-strict) health ;;
  check-qmd) shift; check_qmd "${1:-}" ;;
  start) start_daemon ;;
  ensure) shift; ensure "${1:-}" ;;
  warm) warm ;;
  rotate) rotate ;;
  reload) reload ;;
  kick-index) kick_index ;;
  kick-wiki-compile) shift; kick_wiki_compile "${1:-}" "${2:-}" ;;
  cleanup-legacy) cleanup_legacy ;;
  *) echo "usage: backend_manager.sh identity|health|check-qmd [--manual]|start|ensure [--wait]|warm|rotate|reload|kick-index|kick-wiki-compile <cwd> [--flush]|cleanup-legacy" >&2; exit 2 ;;
esac
