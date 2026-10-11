#!/usr/bin/env bash
# qmd index-on-edit worker. durable claim → 컬렉션 등록 + update + embed → success ACK.
# bash 3.2 호환 (macOS /bin/bash)
set -u

# sandbox는 어떤 부작용(mkdir 포함)도 없이 즉시 무출력 종료.
[ -n "${QMD_SANDBOX:-}" ] && exit 0

_QMD_ROOT="$(cd "$(dirname "$0")/.." && pwd)" || exit 0
. "$_QMD_ROOT/core/qmd_path.sh"

# 유저 격리 경로(멀티유저 /tmp symlink 선점 방지). update.sh와 락 기본값을 통일.
# WRITER_LOCK/EMBED_LOCK 기본값은 update.sh와 반드시 동일해야 직렬화가 유지된다.
_QMD_UID="$(/usr/bin/id -un 2>/dev/null || id -u 2>/dev/null || echo qmd)"
_QMD_LOCK_BASE="${QMD_LOCK_BASE:-${TMPDIR:-/tmp}/qmd-auto-context-locks-${_QMD_UID}}"
_QMD_CACHE_DIR="${QMD_CACHE_DIR:-$HOME/.cache/qmd}"
mkdir -p "$_QMD_CACHE_DIR" "$_QMD_LOCK_BASE" 2>/dev/null || true

QUEUE="${QMD_DIRTY_QUEUE:-$HOME/.config/qmd/dirty-queue}"
WORKER_LOCK="${QMD_INDEX_WORKER_LOCKDIR:-$_QMD_LOCK_BASE/qmd-index-worker.lock.d}"
WRITER_LOCK="${QMD_WRITER_LOCKDIR:-$_QMD_LOCK_BASE/qmd-update.lock.d}"
EMBED_LOCK="${QMD_EMBED_LOCKDIR:-$_QMD_LOCK_BASE/qmd-embed.lock.d}"
# index-worker 동작 로그는 recall 로그(QMD_RECALL_LOG)와 분리한다.
# run-hook이 모든 action에 QMD_RECALL_LOG를 export하므로, 그걸 상속하면
# kick-index 경로에서 worker 로그가 recall 로그 파일로 강제 합쳐진다(그리고 과거엔 /tmp).
# 전용 QMD_INDEX_WORKER_LOG override만 받고, 기본은 유저 격리 캐시 경로.
LOG="${QMD_INDEX_WORKER_LOG:-$_QMD_CACHE_DIR/index-worker.log}"

log() { printf '[%s] index-worker: %s\n' "$(date '+%H:%M:%S')" "$*" >>"$LOG" 2>&1 || true; }

reclaim_dead_lock() {
  local lockdir="$1" holder
  [ -d "$lockdir" ] && [ ! -L "$lockdir" ] || return 1
  holder="$(cat "$lockdir/pid" 2>/dev/null || true)"
  if [ -z "$holder" ]; then
    # Another process may be between mkdir and writing its pid.
    sleep 0.2
    holder="$(cat "$lockdir/pid" 2>/dev/null || true)"
  fi
  [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null && return 1
  [ -z "$holder" ] || rm -f "$lockdir/pid" 2>/dev/null || return 1
  rmdir "$lockdir" 2>/dev/null
}

ack_claim() {
  python3 "$_QMD_ROOT/core/dirty_queue_claim.py" ack "$QUEUE" || {
    log "dirty queue ack failed — claim retained"
    return 1
  }
}

reload_daemon() {
  if [ -n "${QMD_BACKEND_MANAGER:-}" ] && [ -x "$QMD_BACKEND_MANAGER" ]; then
    "$QMD_BACKEND_MANAGER" reload >>"$LOG" 2>&1 || return 1
    return 0
  fi
  log "reload unavailable: QMD_BACKEND_MANAGER missing"
  return 1
}

# PATH 보정 (비대화형 hook 환경; update.sh/backend_manager.sh와 동일)
normalize_qmd_path
if [ -n "${QMD_FAKE_QMD:-}" ] && [ "${QMD_SETUP_GUARD_FIXTURE:-}" = 1 ]; then
  QMD="$QMD_FAKE_QMD"
else
  QMD="$(resolve_qmd_bin 2>/dev/null)" || exit 1
fi
unset BUN_INSTALL; export PATH

# single-flight
if ! mkdir "$WORKER_LOCK" 2>/dev/null; then
  reclaim_dead_lock "$WORKER_LOCK" || exit 0
  mkdir "$WORKER_LOCK" 2>/dev/null || exit 1
fi
echo "$$" > "$WORKER_LOCK/pid" 2>/dev/null || true
trap 'rm -f "$WORKER_LOCK/pid" 2>/dev/null; rmdir "$WORKER_LOCK" 2>/dev/null || true' EXIT

# The durable claim contains the exact queue prefix and is fsynced before QMD
# work. The live queue remains untouched until every stage succeeds.
SNAP="$(mktemp)" || exit 1
python3 "$_QMD_ROOT/core/dirty_queue_claim.py" claim "$QUEUE" "$SNAP"
claim_rc=$?
if [ "$claim_rc" -eq 3 ]; then rm -f "$SNAP"; exit 0; fi
if [ "$claim_rc" -ne 0 ]; then rm -f "$SNAP"; exit 1; fi

# Hold the same writer lock as update.sh through a possible prior daemon
# handoff, queue repair, and all subsequent QMD writes. A reload outside this
# lock could restart the daemon while SessionStart is updating its index.
if ! mkdir "$WRITER_LOCK" 2>/dev/null; then
  if reclaim_dead_lock "$WRITER_LOCK" && mkdir "$WRITER_LOCK" 2>/dev/null; then
    :
  else
    log "writer lock busy — claim retained"
    rm -f "$SNAP"
    exit 75
  fi
fi
echo "$$" > "$WRITER_LOCK/pid" 2>/dev/null || true
trap 'rm -f "$WRITER_LOCK/pid" 2>/dev/null; rmdir "$WRITER_LOCK" 2>/dev/null || true; rm -f "$WORKER_LOCK/pid" 2>/dev/null; rmdir "$WORKER_LOCK" 2>/dev/null || true' EXIT

# Older workers may have left a successful global embed awaiting daemon
# handoff. Complete that obligation before changing the claimed prefix; an
# ownerless row cannot safely be processed again under the shared index.
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  python3 "$_QMD_ROOT/core/dirty_queue_claim.py" needs-reload "$QUEUE" >/dev/null 2>&1
  pending_reload_rc=$?
  if [ "$pending_reload_rc" -eq 0 ]; then
    reload_daemon || { log "legacy reload failed — claim retained"; rm -f "$SNAP"; exit 1; }
    python3 "$_QMD_ROOT/core/dirty_queue_claim.py" reload-done "$QUEUE" >/dev/null 2>&1
    reload_done_rc=$?
    [ "$reload_done_rc" -eq 3 ] || {
      log "legacy reload receipt failed — claim retained"; rm -f "$SNAP"; exit 1;
    }
  elif [ "$pending_reload_rc" -ne 3 ]; then
    log "legacy reload state invalid — claim retained"; rm -f "$SNAP"; exit 1
  fi
fi

# Old two-column entries cannot prove collection ownership in the selected
# shared store. Preserve their exact bytes in a private quarantine, then
# reclaim the remaining owner-bound entries. A done claim makes the archive
# and queue replacement crash-resumable; no historical entry is discarded.
legacy_pass=0
while [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; do
  python3 "$_QMD_ROOT/core/dirty_queue_claim.py" quarantine-ownerless "$QUEUE" >/dev/null 2>&1
  legacy_rc=$?
  [ "$legacy_rc" -eq 3 ] && break
  if [ "$legacy_rc" -ne 0 ]; then log "legacy dirty queue quarantine failed — claim retained"; rm -f "$SNAP"; exit 1; fi
  legacy_pass=$((legacy_pass + 1))
  if [ "$legacy_pass" -gt 8 ]; then log "legacy dirty queue quarantine limit — claim retained"; rm -f "$SNAP"; exit 1; fi
  python3 "$_QMD_ROOT/core/dirty_queue_claim.py" claim "$QUEUE" "$SNAP"
  claim_rc=$?
  if [ "$claim_rc" -eq 3 ]; then rm -f "$SNAP"; exit 0; fi
  if [ "$claim_rc" -ne 0 ]; then rm -f "$SNAP"; exit 1; fi
done

# A previous run may have embedded successfully but failed daemon handoff.
# Retain this fact across process restarts and incremental 0/0 retries.
PRIOR_RELOAD_REQUIRED=0
python3 "$_QMD_ROOT/core/dirty_queue_claim.py" needs-reload "$QUEUE" >/dev/null 2>&1
reload_state_rc=$?
if [ "$reload_state_rc" -eq 0 ]; then PRIOR_RELOAD_REQUIRED=1
elif [ "$reload_state_rc" -ne 3 ]; then rm -f "$SNAP"; exit 1
fi
mark_reload_required() { python3 "$_QMD_ROOT/core/dirty_queue_claim.py" reload-required "$QUEUE"; }
mark_reload_done() { python3 "$_QMD_ROOT/core/dirty_queue_claim.py" reload-done "$QUEUE" || [ "$?" -eq 3 ]; }

# dedupe (name\tpath) — bash 3.2 호환 (mapfile 미사용)
ENTRIES=()
while IFS= read -r line; do
  [ -n "$line" ] && ENTRIES+=("$line")
done < <(sort -u "$SNAP")
rm -f "$SNAP"
[ "${#ENTRIES[@]}" -eq 0 ] && { ack_claim; exit $?; }

# Resolve every dirty collection before any QMD write. Mixed project queues
# must never be handed to a single global-index update.
ROUTES=()
has_project_index=0
for e in "${ENTRIES[@]}"; do
  remainder="${e#*$'\t'}"
  path="${remainder%%$'\t'*}"
  owner=""
  [[ "$remainder" == *$'\t'* ]] && owner="${remainder#*$'\t'}"
  if ! python3 "$_QMD_ROOT/core/setup_guard.py" check "${owner:-$path}" >/dev/null 2>&1; then
    log "project setup required — claim retained"
    exit 1
  fi
  route="$(python3 "$_QMD_ROOT/core/qmd_route.py" resolve-env "${owner:-$path}")" || {
    log "invalid project index pointer — claim retained"
    exit 1
  }
  if [ -n "$owner" ] && [ -z "$route" ]; then
    log "selected project index missing — claim retained"
    exit 1
  fi
  if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
    [ -n "$owner" ] && python3 "$_QMD_ROOT/core/qmd_route.py" register-config "$owner" >/dev/null 2>&1 || {
      log "collection owner registration failed — claim retained"
      exit 1
    }
    name="${e%%$'\t'*}"
    python3 "$_QMD_ROOT/core/qmd_route.py" verify-collection "$owner" "$name" "$path" >/dev/null 2>&1 || {
      log "collection owner mismatch — claim retained"
      exit 1
    }
  fi
  ROUTES+=("$route")
  [ -n "$route" ] && has_project_index=1
done
if ! python3 "$_QMD_ROOT/core/qmd_route.py" verify-shared-enrollment >/dev/null 2>&1; then
  log "registered shared collection withdrew or changed — claim retained"
  exit 1
fi
if [ "$has_project_index" = 1 ]; then
  # Selected project stores write their own QMD index, but still share this
  # embed process with SessionStart maintenance. Take the same embed lock so
  # a post-reconcile kick cannot race the detached update/embed child.
  if [ -d "$EMBED_LOCK" ]; then
    epid="$(cat "$EMBED_LOCK/pid" 2>/dev/null || true)"
    { [ -z "$epid" ] || ! kill -0 "$epid" 2>/dev/null; } && {
      rm -f "$EMBED_LOCK/pid" 2>/dev/null
      rmdir "$EMBED_LOCK" 2>/dev/null || true
    }
  fi
  if ! mkdir "$EMBED_LOCK" 2>/dev/null; then
    log "embed lock busy — selected claim retained"
    exit 75
  fi
  echo "$$" > "$EMBED_LOCK/pid" 2>/dev/null || true
  trap 'rm -f "$EMBED_LOCK/pid" 2>/dev/null; rmdir "$EMBED_LOCK" 2>/dev/null || true; rm -f "$WRITER_LOCK/pid" 2>/dev/null; rmdir "$WRITER_LOCK" 2>/dev/null || true; rm -f "$WORKER_LOCK/pid" 2>/dev/null; rmdir "$WORKER_LOCK" 2>/dev/null || true' EXIT
  # The existing writer lock is held throughout. Each subshell receives only
  # its own project index environment; no project DB can absorb another queue.
  touched_global=0
  for route in "${ROUTES[@]}"; do [ -z "$route" ] && touched_global=1; done
  if { [ "$touched_global" = 1 ] || [ "$PRIOR_RELOAD_REQUIRED" = 1 ]; } && [ -z "${QMD_NO_RELOAD:-}" ]; then
    mark_reload_required || exit 1
  fi
  failed=0
  for i in "${!ENTRIES[@]}"; do
    e="${ENTRIES[$i]}"; name="${e%%$'\t'*}"
    remainder="${e#*$'\t'}"; path="${remainder%%$'\t'*}"
    route="${ROUTES[$i]}"
    [ -z "$route" ] && touched_global=1
    [ -d "$path" ] || continue
    if ! (
      if [ -n "$route" ]; then
        IFS=$'\t' read -r INDEX_PATH QMD_CONFIG_DIR XDG_CACHE_HOME <<< "$route"
        export INDEX_PATH QMD_CONFIG_DIR XDG_CACHE_HOME
      fi
      add_out="$("$QMD" collection add "$path" --name "$name" 2>&1)" || {
        printf '%s\n' "$add_out" | grep -qi 'already exists' || exit 1
      }
      python3 "$_QMD_ROOT/core/qmd_route.py" verify-shared-enrollment >/dev/null 2>&1 && \
        "$QMD" update >>"$LOG" 2>&1 && \
        python3 "$_QMD_ROOT/core/qmd_route.py" verify-shared-enrollment >/dev/null 2>&1 && \
        "$QMD" embed >>"$LOG" 2>&1
    ); then
      log "project-routed update failed — claim retained: $name"
      failed=1
    fi
  done
  [ "$failed" = 0 ] || exit 1
  if [ -z "${QMD_NO_RELOAD:-}" ] && { [ "$touched_global" = 1 ] || [ "$PRIOR_RELOAD_REQUIRED" = 1 ]; }; then
    reload_daemon || exit 1
    mark_reload_done || exit 1
  fi
  ack_claim || exit $?
  # Same-event PostToolUse workers may have arrived before QMD settled the
  # edited file. Only an ACKed owner route can wake the exact synthetic pilot;
  # the helper independently verifies current scope, approval and pending edit.
  WAKE_OWNERS=()
  for e in "${ENTRIES[@]}"; do
    remainder="${e#*$'\t'}"
    [[ "$remainder" == *$'\t'* ]] || continue
    owner="${remainder#*$'\t'}"
    [ -n "$owner" ] || continue
    already_woken=0
    if [ "${#WAKE_OWNERS[@]}" -gt 0 ]; then
      for prior in "${WAKE_OWNERS[@]}"; do
        [ "$prior" = "$owner" ] && already_woken=1
      done
    fi
    [ "$already_woken" = 1 ] && continue
    WAKE_OWNERS+=("$owner")
    python3 "$_QMD_ROOT/core/context_learning_shared.py" wake-after-index "$owner" >/dev/null 2>>"$LOG" || \
      log "synthetic post-index wake failed"
  done
  exit 0
fi

# Mark before QMD can alter the index. A crash before handoff conservatively
# causes one extra reload; a crash after an embed cannot lose the obligation.
[ -n "${QMD_NO_RELOAD:-}" ] || mark_reload_required || exit 1
added=0
failed=0
for e in "${ENTRIES[@]}"; do
  name="${e%%	*}"; path="${e#*	}"
  [ -n "$name" ] && [ -n "$path" ] || continue
  if [ ! -d "$path" ]; then log "skip missing dir: $name -> $path"; continue; fi
  if out=$("$QMD" collection add "$path" --name "$name" 2>&1); then
    added=1
  elif printf '%s' "$out" | grep -qi "already exists"; then
    added=1
  else
    failed=1
  fi
  printf '%s\n' "$out" >>"$LOG"
done
[ "$failed" = 0 ] || { log "collection add failed — claim retained"; exit 1; }
if [ "$added" = 0 ]; then
  if [ -z "${QMD_NO_RELOAD:-}" ] && [ "$PRIOR_RELOAD_REQUIRED" = 1 ]; then reload_daemon || exit 1; fi
  [ -n "${QMD_NO_RELOAD:-}" ] || mark_reload_done || exit 1
  ack_claim; exit $?
fi

if ! UPDATE_OUT="$("$QMD" update 2>&1)"; then
  printf '%s\n' "$UPDATE_OUT" >>"$LOG"; log "update failed — claim retained"; exit 1
fi
printf '%s\n' "$UPDATE_OUT" >>"$LOG"

# embed lock 획득 (update.sh 백그라운드 embed와 동시 실행 방지)
# stale 방어: pid liveness 체크 (update.sh 프로토콜과 대칭)
if [ -d "$EMBED_LOCK" ]; then
  epid="$(cat "$EMBED_LOCK/pid" 2>/dev/null || true)"
  { [ -z "$epid" ] || ! kill -0 "$epid" 2>/dev/null; } && { rm -f "$EMBED_LOCK/pid" 2>/dev/null; rmdir "$EMBED_LOCK" 2>/dev/null || true; }
fi
if ! mkdir "$EMBED_LOCK" 2>/dev/null; then
  log "embed lock busy — claim retained"
  exit 75
fi
echo "$$" > "$EMBED_LOCK/pid" 2>/dev/null || true
trap 'rm -f "$EMBED_LOCK/pid" 2>/dev/null; rmdir "$EMBED_LOCK" 2>/dev/null || true; rm -f "$WRITER_LOCK/pid" 2>/dev/null; rmdir "$WRITER_LOCK" 2>/dev/null || true; rm -f "$WORKER_LOCK/pid" 2>/dev/null; rmdir "$WORKER_LOCK" 2>/dev/null || true' EXIT

# embed (전체 incremental). 출력에서 새 임베딩 수 파싱.
if ! EMBED_OUT="$("$QMD" embed 2>&1)"; then
  printf '%s\n' "$EMBED_OUT" >>"$LOG"
  log "embed failed — claim retained"
  exit 1
fi
printf '%s\n' "$EMBED_OUT" >>"$LOG"
NEW=$(printf '%s' "$EMBED_OUT" | grep -oE 'Embedded [0-9]+ chunks' | grep -oE '[0-9]+' | head -1)
NEW="${NEW:-0}"
REMOVED=$(printf '%s' "$UPDATE_OUT" | grep -oE '[1-9][0-9]* removed' | grep -oE '^[0-9]+' | head -1)
REMOVED="${REMOVED:-0}"

# The durable claim carries an earlier failed reload through an incremental
# 0/0 retry. Clear it only after a successful handoff.
if [ -z "${QMD_NO_RELOAD:-}" ]; then
  if [ "$PRIOR_RELOAD_REQUIRED" = 1 ] || [ "$NEW" -gt 0 ] || [ "$REMOVED" -gt 0 ]; then
    reload_daemon || exit 1
  fi
  mark_reload_done || exit 1
fi
ack_claim
exit $?
