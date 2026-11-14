"""Deduplication judgment outcomes."""

OUTCOME_OK = "ok"
OUTCOME_UNAVAILABLE = "unavailable"

ATTRIBUTION_JOB = "job"    # 생산 엔진이 큐 잡에서 온 사실 (write-time gate)
ATTRIBUTION_NONE = "none"  # 카드 두 장, 둘째 생산자 미기록 (retroactive scan)
