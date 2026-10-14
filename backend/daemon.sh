#!/bin/bash
# managed-by: qmd-auto-context
# qmd HTTP MCP 데몬 런처 (plugin runtime manager에서 foreground 실행).
#
# 배경: 홈 node_modules의 better-sqlite3 네이티브 모듈은 특정 Node ABI(MODULE_VERSION)로만
# 빌드돼 있어, 맞지 않는 node로 qmd를 실행하면 `ERR_DLOPEN_FAILED`로 즉사한다.
# fnm 설치 node 버전이 여러 개이고 default alias가 어디를 가리킬지 보장되지 않으므로,
# 버전을 하드코딩하지 않고(=CLAUDE.md 원칙) 후보 node들로 better-sqlite3 native load를 probe 하여
# ABI가 맞는 첫 node를 런타임에 선택한다.
#
# foreground(`mcp --http`, --daemon 아님)로 실행해야 manager가 프로세스를 추적/재기동한다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/core/qmd_path.sh"
normalize_qmd_path
QMD_BIN="$(resolve_qmd_bin 2>/dev/null)" || {
  echo "[qmd-daemon] FATAL: QMD runtime selection failed" >&2
  exit 1
}
PORT="${QMD_DAEMON_PORT:-8584}"
FNM_ROOT="$HOME/.local/share/fnm/node-versions"

# qmd 의 bin/qmd 런처는 dist/cli/qmd.js 를 child 로 spawn 한다(2단 프로세스).
# 그러면 manager가 런처(직속 자식)만 감시하고 실제 서버(손자)가 죽어도 복구를 보장 못 한다.
# 런처를 따라가 dist/cli/qmd.js 를 직접 exec 하여 single 프로세스로 만든다(manager가 서버를 직접 감시).
QMD_SPEC="$(QMD_BIN="$QMD_BIN" python3 "$ROOT/core/qmd_route.py" daemon-spec)" || {
  echo "[qmd-daemon] FATAL: QMD entry selection failed" >&2
  exit 1
}
IFS=$'\x1f' read -r QMD_ENTRY PINNED_NODE QMD_PACKAGE_BIN <<< "$QMD_SPEC"
if [ "${QMD_SETUP_GUARD_FIXTURE:-}" != 1 ]; then
  QMD_SHARED_ENV="$(python3 "$ROOT/core/qmd_route.py" resolve-shared-env)" || exit 1
  IFS=$'\t' read -r INDEX_PATH QMD_CONFIG_DIR XDG_CACHE_HOME <<< "$QMD_SHARED_ENV"
  export INDEX_PATH QMD_CONFIG_DIR XDG_CACHE_HOME HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
fi

# qmd bin/qmd 런처는 MCP 모드에서 native quiet/Metal env 를 세팅한다(dist/cli/qmd.js 직접 실행 시 누락됨).
# 이 누락은 단순 로그 노이즈가 아니라 실제 vec 쿼리 성능 저하(GGML_METAL_NO_RESIDENCY 미설정 등)를 유발하므로
# 런처와 동일하게 복제한다. (참조: bin/qmd 의 `if (process.argv[2] === "mcp")` 블록)
export LLAMA_LOG_LEVEL="${LLAMA_LOG_LEVEL:-error}"
export GGML_LOG_LEVEL="${GGML_LOG_LEVEL:-error}"
export GGML_BACKEND_SILENT="${GGML_BACKEND_SILENT:-1}"
if [ "$(uname)" = "Darwin" ] && [ "${QMD_METAL_KEEP_RESIDENCY:-}" != "1" ]; then
  export GGML_METAL_NO_RESIDENCY="${GGML_METAL_NO_RESIDENCY:-1}"
fi

probe_node_abi() {
  local candidate="$1"
  [ -x "$candidate" ] || return 1
  # Resolve the native module relative to this QMD installation. A private
  # node_modules tree need not expose better-sqlite3 from $HOME.
  "$candidate" - "$QMD_PACKAGE_BIN" <<'JS' >/dev/null 2>&1
const { createRequire } = require('node:module');
createRequire(process.argv[2])('better-sqlite3');
JS
}

pick_compatible_node_bin() {
  local bin
  if [ -n "$PINNED_NODE" ]; then
    probe_node_abi "$PINNED_NODE" || return 1
    printf '%s\n' "$PINNED_NODE"
    return 0
  fi
  if [ -n "${QMD_NODE_BIN:-}" ]; then
    probe_node_abi "$QMD_NODE_BIN" || return 1
    printf '%s\n' "$QMD_NODE_BIN"
    return 0
  fi
  for bin in $(ls -d "$FNM_ROOT"/v*/installation/bin 2>/dev/null | sort -rV); do
    probe_node_abi "$bin/node" || continue
    printf '%s\n' "$bin/node"
    return 0
  done
  return 1
}

NODE_EXEC="$(pick_compatible_node_bin || true)"
if [ -n "$NODE_EXEC" ]; then
  echo "[qmd-daemon] ABI 호환 node 선택: $NODE_EXEC" >&2
else
  # ABI 호환 node를 못 찾으면 데몬은 어차피 ERR_DLOPEN_FAILED 로 즉사한다. 명확히 실패시킨다.
  echo "[qmd-daemon] FATAL: better-sqlite3 ABI 호환 node를 QMD_NODE_BIN 또는 fnm($FNM_ROOT)에서 찾지 못함. 데몬을 띄우지 않음." >&2
  exit 1
fi

# manager가 nohup으로 실행하므로 stdout/stderr는 manager가 지정한 daemon log로 간다.
echo "[qmd-daemon] starting: node=$NODE_EXEC entry=$QMD_ENTRY port=$PORT" >&2
exec "$NODE_EXEC" "$QMD_ENTRY" mcp --http --port "$PORT"
