#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as qmd_config
import cooldown as qmd_cooldown
import resolve_paths as rp

GATED_TOOLS = {"Edit", "Write", "apply_patch", "MultiEdit", "NotebookEdit"}
SKIP_TTL_SECONDS = 2 * 60 * 60  # 2시간


def is_sandbox():
    # CODEX_SANDBOX is deliberately absent: ordinary Codex workspace-write runs
    # set it, and pending-project edits must still be denied there. Nested
    # extractor isolation is marked by QMD_SANDBOX (core/extractors/lib.py).
    return bool(os.environ.get("QMD_SANDBOX") or os.environ.get("GEMINI_SANDBOX")
                or os.environ.get("CLAUDE_HEADLESS") == "1")


def has_skip_marker(cwd):
    """skip 마커 파일 존재 + TTL 확인. TTL 만료 시 lazy unlink 후 False 반환.

    TTL 판정은 `cooldown.window_elapsed`가 SSOT다 — `time.time() - mtime`을 그대로 쓰면
    **미래 mtime**(시계 되돌림·백업 복원)에서 나이가 음수가 되어 마커가 만료되지 않고
    **2h TTL이 무기한**이 된다(= pending 프로젝트의 편집 gate가 영구 우회된다).
    """
    real_cwd = os.path.realpath(cwd)
    h = hashlib.sha256(real_cwd.encode()).hexdigest()
    marker = Path.home() / ".config" / "qmd" / "skip" / h
    if not marker.exists():
        return False
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return False
    if qmd_cooldown.window_elapsed(mtime, time.time(), SKIP_TTL_SECONDS):
        # lazy expire: TTL 지난 마커 unlink
        try:
            marker.unlink()
        except OSError:
            pass
        return False
    return True


def main():
    if is_sandbox():
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    tool = payload.get("tool_name", "")
    if tool not in GATED_TOOLS:
        return 0  # Read/Bash 등은 차단 안 함 (matcher가 1차 필터, 여기 2차 방어)
    cwd = payload.get("cwd") or os.getcwd()
    try:
        config = qmd_config.load_project_config(cwd)
        result = rp.resolve_paths(cwd, json.dumps(config))
    except Exception:
        # gate는 soft protection이지 하드 보안 경계가 아니다 -- 샌드박스/권한 등
        # 예상 못한 환경 차이로 config 조회가 실패해도 hook 프로세스 자체가
        # non-zero exit로 죽어 편집을 막는 것보다 fail-open이 낫다.
        return 0
    reason = result.get("reason")
    # pending이 아니면(동의/거절/risky/정상) 통과. pending이어도 skip이면 통과.
    if reason != "pending":
        _snapshot_allowed_edit(payload)
        return 0
    if has_skip_marker(cwd):
        _snapshot_allowed_edit(payload)
        return 0
    msg = ("⛔ qmd-auto-context: 이 프로젝트의 검색 인덱스 사용 여부가 아직 결정되지 않아 편집을 보류합니다. "
           "사용자에게 자연어로 추천 범위 확인, 원하는 범위로 켜기, 끄기, 또는 2시간 미루기 중 선택을 요청하세요. "
           "설치된 setup 스킬로 상태와 범위를 확인할 수 있습니다. 사용자 선택 전 다른 편집 경로로 재시도하지 마세요. "
           "읽기·검색은 가능합니다.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": msg,
    }}, ensure_ascii=False))
    return 0


def _snapshot_allowed_edit(payload):
    """Only an allowed real host edit may create a deletion preimage."""
    try:
        from edit_context import before
        before(payload)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        pass  # Ordinary projects and unsupported edit payloads stay unchanged.


if __name__ == "__main__":
    import hook_main
    raise SystemExit(hook_main.run(main))
