#!/usr/bin/env python3
"""시간 기반 게이트(cooldown·TTL·stale lock) 판정의 SSOT.

**왜 한 벌인가**: 이 저장소에서 "구현이 두 벌로 갈려 한쪽만 고쳐졌다"가 반복됐고
(`write_text_atomic`, `wiki_root_of`, dedup 게이트), 시간 게이트는 그중에서도 실패가
가장 조용한 종류다 — 잘못 판정하면 아무 로그도 남기지 않고 **그 파이프라인이 영구
정지**한다. 그래서 판정 규칙 두 개를 여기 한 곳에 둔다.

규칙 1 — 만료 시각을 파일에 적는 형태(`expiry_active`). `float(파일내용)` 뒤 `now <
expiry`만 보면 오염된 값(`1e300`) 하나가 **영구 활성**이 되어 그 게이트가 다시 열리지
않는다(compile cooldown = compile 영구 정지, dedup judge cooldown = 판정 영구 정지).
그래서 상한(`MAX_COOLDOWN_SECS`)을 넘는 만료값은 식힘으로 보지 않는다 —
`wiki_compile_worker.load_engine_cooldowns`가 4단계에서 받은 처방과 **같은 규칙**이고,
그 상수도 여기서 가져간다(리터럴을 흩지 않는다).

규칙 2 — 파일 mtime의 나이를 보는 형태(`window_elapsed`). `now - mtime`을 그대로 쓰면
**미래 mtime**(시계 되돌림·백업 복원·파일시스템 이관으로 실제로 생긴다)에서 나이가
음수가 되어 창이 영원히 끝나지 않는다. 방향은 호출부마다 다르지만 병리는 같다:
dedup scan은 영구 skip, orphan 회수는 영구 skip, sync lock은 stale 회수 불가,
`notice_once`는 모든 알림 영구 억제, skip 마커는 gate 영구 우회.

미래 mtime을 "즉시 만료"로 보지 않고 `FUTURE_SKEW_TOLERANCE_SECS`만큼 관용하는 이유:
NTP 미세 조정·파일시스템 타임스탬프 반올림으로 몇 밀리초 앞선 mtime은 정상이고, 그것을
만료로 읽으면 유료 호출을 한 번 더 하는 방향으로 틀린다. 관용 범위를 넘는 값은
"쓸 수 없는 타임스탬프"라 창이 끝난 것으로 취급한다(창을 영구화하는 것보다 한 번 더
시도하는 것이 항상 낫다 — 이 저장소의 fail-open 방향과 같다).
"""
from __future__ import annotations

# 만료 시각 상한. 이 값을 넘어선 만료값은 오염된 것으로 보고 무시한다. 쓰기 쪽도 같은
# 값으로 클램프하므로(`clamp_seconds`) 정상 경로는 절대 상한을 넘지 않는다.
# `wiki_compile_worker.MAX_ENGINE_COOLDOWN_SECS`가 이 상수의 별칭이다.
MAX_COOLDOWN_SECS = 86400

# 미래 mtime 관용 범위(초). 이보다 더 앞선 타임스탬프는 쓸 수 없는 값으로 본다.
FUTURE_SKEW_TOLERANCE_SECS = 60

# 잡히기만 하고 풀리지 않는 lock 디렉터리를 stale 로 보는 나이. 저장소 관례를 따른다 —
# `backend/keepalive.sh`, `backend/index_worker.sh`, `core/backend_manager.sh`(3곳)이
# 전부 `find "$LOCK" -maxdepth 0 -mmin +10`이다. 파이썬 쪽 lock도 같은 임계를 쓴다.
LOCK_STALE_SECS = 600


def clamp_seconds(seconds) -> int:
    """cooldown 길이를 `0..MAX_COOLDOWN_SECS`로 자른다(쓰기 쪽 가드)."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return 0
    return min(max(0, value), MAX_COOLDOWN_SECS)


def age_seconds(mtime, now):
    """진단용 나이(초). **판정에 쓰지 말 것** — 판정은 `window_elapsed`가 SSOT다.
    이 값은 로그·`--json`에 "그 lock/마커가 얼마나 오래됐는가"를 남기는 용도이고,
    그 표면화가 `lock_busy`의 일시/영구를 구분하는 유일한 신호다."""
    try:
        return round(float(now) - float(mtime), 1)
    except (TypeError, ValueError):
        return None


def window_elapsed(mtime, now, seconds) -> bool:
    """mtime 기준 `seconds` 창이 끝났는가(또는 mtime이 쓸 수 없는 값인가).

    반환값의 방향은 호출부가 정한다(창이 끝났으면 실행 / 창이 끝났으면 stale 회수).
    """
    try:
        age = float(now) - float(mtime)
    except (TypeError, ValueError):
        return True  # 타임스탬프를 못 읽으면 창을 영구화하지 않는다
    if age != age:  # NaN
        return True
    if age < -FUTURE_SKEW_TOLERANCE_SECS:
        return True  # 미래 mtime: 창을 영원히 열어 두는 대신 끝난 것으로 본다
    if age < 0:
        age = 0.0  # 관용 범위 안의 시계 오차는 "방금 쓴 것"으로 본다
    try:
        window = max(0, int(seconds))
    except (TypeError, ValueError):
        return True
    return age >= window


def expiry_active(raw, now) -> bool:
    """파일에 적힌 만료 시각이 아직 유효한가. 오염값·미래 과다값은 False."""
    try:
        expiry = float(raw)
    except (TypeError, ValueError):
        return False
    if expiry != expiry:  # NaN — 비교가 전부 False라 어차피 안 걸리지만 명시해 둔다
        return False
    try:
        moment = float(now)
    except (TypeError, ValueError):
        return False
    # 상한 초과(`1e300`, `inf`)는 식힘으로 보지 않는다 — 영구 활성 방지.
    return moment < expiry <= moment + MAX_COOLDOWN_SECS


def expiry_value(now, seconds) -> float:
    """쓰기 쪽: 클램프된 만료 시각. `expiry_active`의 상한과 짝이다."""
    return float(now) + clamp_seconds(seconds)
