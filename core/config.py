#!/usr/bin/env python3
import argparse
import json
import math
import sys
from pathlib import Path


CONFIG_DIR_NAME = ".auto-context"
SETTINGS_FILE_NAME = "settings.json"

# ---------------------------------------------------------------------------
# Collection roles (SSOT)
#
# 네 role은 **두 축의 조합**이다: qmd 인덱스에 등록되는가(=recall 대상), wiki compile의
# 입력이 되는가. `source`는 "compile 입력이지만 인덱싱·recall 대상 아님"을 표현하기 위해
# 도입됐다(로드맵 7단계) — raw를 인덱스에서 빼면서도 그 문서로 카드를 계속 만들기 위한
# 선행 작업이다.
#
#   role      | qmd 등록/인덱싱/recall | compile 입력 | hierarchical raw backfill
#   ----------|------------------------|--------------|--------------------------
#   raw       | O                      | O            | O
#   session   | O                      | O            | O
#   wiki      | O                      | X            | X (wiki phase에서 먼저 질의)
#   source    | X                      | O            | X
#   (미지 값) | X                      | X            | X  ← fail-closed, 아래 참고
#
# 판정을 `roles.get(c) != "wiki"` 같은 **여집합**으로 쓰지 말 것. 세 번째 값이 들어오는
# 순간 그 지점 전부가 오분류한다. 아래 집합과 헬퍼(`collection_role` 외)를 쓴다.
COLLECTION_ROLE_RAW = "raw"
COLLECTION_ROLE_WIKI = "wiki"
COLLECTION_ROLE_SESSION = "session"
COLLECTION_ROLE_SOURCE = "source"
COLLECTION_ROLES = frozenset({
    COLLECTION_ROLE_RAW,
    COLLECTION_ROLE_WIKI,
    COLLECTION_ROLE_SESSION,
    COLLECTION_ROLE_SOURCE,
})
# **키 없음과 값 미지를 구분한다.**
#   키 없음  → `raw`(role 도입 전 동작). 하위호환이므로 fail-open이 맞다.
#   값 미지  → `invalid` 센티널. 어떤 집합에도 속하지 않아 인덱싱·recall·compile에서
#              전부 빠진다(fail-closed).
# 구분하는 이유: 키가 있다는 것은 **사용자가 무언가를 의도했는데 우리가 못 읽었다**는
# 뜻이다. 거기서 `raw`로 fail-open하면 `"sourse"` 오타 하나가 "색인 제외"를 실제 색인으로
# 뒤집어 사용자 데이터가 의도치 않게 인덱싱된다(실측: `collection add`가 그대로 실행됐다).
# 이 센티널은 **설정 값이 아니다** — `COLLECTION_ROLES`에 넣지 말 것. 대신
# `collection_role_map`이 미지 값을 이 값으로 정규화해 raw settings와 normalize된
# config가 **같은 판정**을 내게 한다(예전엔 normalize가 미지 항목을 통째로 버려
# "키 없음"으로 보였고, 그래서 recall/index_enqueue 같은 정규화 소비자에서만 fail-open이
# 되살아났다). fail-closed는 조용하면 안 되므로 종점은 `invalid_role_collections` →
# `update.sh`의 SessionStart notice다.
COLLECTION_ROLE_INVALID = "invalid"
DEFAULT_COLLECTION_ROLE = COLLECTION_ROLE_RAW
# qmd `collection add`/`update`/`embed`, dirty queue, recall 질의 대상.
INDEXED_ROLES = frozenset({COLLECTION_ROLE_RAW, COLLECTION_ROLE_WIKI, COLLECTION_ROLE_SESSION})
# hierarchical raw backfill / flat recall의 non-wiki 대상(= 인덱싱되는 non-wiki).
RECALL_RAW_ROLES = frozenset({COLLECTION_ROLE_RAW, COLLECTION_ROLE_SESSION})
# wiki compile source가 될 수 있는 role.
COMPILE_SOURCE_ROLES = frozenset({
    COLLECTION_ROLE_RAW,
    COLLECTION_ROLE_SESSION,
    COLLECTION_ROLE_SOURCE,
})
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "name": "",
    "collections": [],
    "minScore": 0.0,
    "rawFallbackMinScore": 0.0,
    "topN": 3,
    "queryTimeout": 5,
    "staleQueueThreshold": 20,
    "lexicalPatterns": [],
    "skipPaths": [],
    "collectionPaths": {},
    "allowRoots": [],
    "prefixStyle": "full",
    # wiki 카드 본문(요약)을 recall 주입에 넣을 때의 카드 1장당 문자 상한. 0이면 본문
    # 주입을 끄고 예전처럼 경로+title만 넣는다. 기본값 근거는 docs/settings.md 참고.
    "injectSummaryMaxChars": 600,
    # wiki 카드의 원문 경로(`sources[].path`)를 카드 1장당 몇 개까지 주입할지. 0이면 끈다.
    # 기본값 근거는 docs/settings.md 참고(849장 전수: median 1 / p95 1 / max 4).
    "injectSourcePathsPerCard": 3,
    "events": ["sessionStart", "userPromptSubmit", "postToolUse"],
    # 파일 스냅샷 감지/인덱스 큐잉만 제어한다. 위키 컴파일 동의와 독립이다.
    "sessionStartSync": True,
    "indexing": None,
    "collectionRoles": {},
    "recallStrategy": "hierarchical",
    "wikiPath": ".auto-context/wiki",
    # qmd 인덱스 자체의 유지보수(전역 인덱스 대상). 프로젝트 설정에 두는 이유는
    # update.sh(SessionStart)가 프로젝트 단위로 돌기 때문이고, `qmd update`/`embed`가
    # 이미 같은 성질(프로젝트 세션이 전역 인덱스를 만진다)이다. 기본 on 근거는
    # docs/settings.md "orphan 벡터 자동 회수".
    "maintenance": {
        "orphanVectors": {
            "enabled": True,
            "minRatio": 0.2,
            "minCount": 200,
            "cooldownSeconds": 86400,
        },
    },
    "compile": {
        # **활성 스위치는 이 값 하나다.** 예전에는 `enabled`(bool) + `mode`(4값) +
        # `autoWrite`(bool)가 같은 사실을 세 곳에서 표현했고, 게이트 6곳의 판정이
        # 균일하지 않아 `enabled:true + mode:"off"`에서 dedup scan과 SessionStart
        # notice만 도는 비대칭이 있었다. `mode != "off"`가 곧 활성이다.
        "mode": "off",
        # 값 집합은 `COMPILE_DEFAULT_STATUSES`(generated|tentative)로 좁다 —
        # `verified`를 넣으면 검수되지 않은 카드가 `recallVerifiedOnly` 기본값 아래에서
        # 캐논으로 인용된다(기계 검수를 우회하는 유일한 구멍이었다).
        "defaultStatus": "generated",
        "excludeStatusesFromRecall": ["discarded", "contested"],
        "lowPriorityStatuses": ["generated", "tentative"],
        "recallVerifiedOnly": True,
        "triggers": [],
        "maxAutoPageLines": 120,
        "maxSourceChars": 12000,
        # wiki 수명 주기(`core/wiki_lifecycle.py`). 카드 판정 자체는 유료 호출이 없어
        # budget 블록에 두지 않는다(재생성 enqueue는 compile triggers 동의를 따른다).
        # `enabled:false`는 update worker의 수명 주기 조치(삭제·enqueue)를 끈다.
        "sourceScan": {
            "enabled": True,
            # 한 회차의 창. 초과분은 순환 커서가 다음 회차에 이어 본다(유실 없음).
            "maxCardsPerScan": 300,
        },
        "reasoningEffort": {
            "generation": "low",
            "verify": "medium",
            "semanticDedup": "medium",
            "engines": {},
        },
        "extractor": {
            # 심볼릭 엔진 이름만 (adapter 경로 금지 — worker가 런타임에 해석한다).
            # `dispatch: "by-engine"` 게이트는 제거됐다: 그 키가 없으면 builtins/backends가
            # 통째로 무시돼 잡이 `missing_extractor`로 **폐기**됐고(requeue 아님),
            # "by-engine이 아닌 dispatch"라는 다른 값은 존재한 적이 없다.
            "backends": {},
            "builtins": [],
            # 120초. 30이던 동안 이 값을 명시하지 않는 writer(`--init-wiki` novel preset)로
            # 온보딩한 프로젝트는 compile을 켜 놓고도 adapter 호출이 매번 timeout →
            # transient → cooldown 루프에 빠졌고, 훅이 무출력이라 증상이 보이지 않았다.
            # compile을 실제로 쓰는 writer·라이브 프로젝트는 전부 이미 120이다.
            "timeout": 120,
            "cooldownSeconds": 600,
        },
        # **유료 host CLI 호출 수를 정하는 값을 한 블록에 모은다.** 한 run의 호출 총량은
        # 이들의 **곱**인데(extractorPerRun × cardsPerSource가 카드 수 = judge 호출 수)
        # 예전에는 4개 서브트리에 흩어져 있었고, 특히 `batch.maxItems`(시작 조건)와
        # `batch.maxPerRun`(상한)의 동거가 혼동원이었다. 곱 자체의 총량 상한은 두지
        # 않는다(두 값을 함께 올리는 것은 사용자의 명시적 선택이다 — docs/settings.md).
        "budget": {
            "extractorPerRun": 10,   # run당 extractor spawn 상한 (초과분은 requeue)
            "cardsPerSource": 10,    # 소스당 카드 수 상한 (모델 출력 상한)
            "verifyPerRun": 3,       # 사람이 정하는 검수 예산의 하한
            "dedupPairsPerScan": 8,  # retroactive scan 1회의 judge 호출 상한
            "dedupPairsPerCompile": 1,  # write-time gate 1회의 judge 호출 상한
        },
        # **처리 시작 조건만 남는다**(상한은 budget). maxItems는 "이만큼 모이면 지금
        # 돌려라"이고 idleSeconds는 "가장 오래된 잡이 이만큼 묵으면 돌려라"다.
        "batch": {
            "idleSeconds": 90,
            "maxItems": 5,
        },
        "semanticDedup": {
            "enabled": True,
            # 무료 score gate의 scan당 쌍 상한(유료 judge 예산은 budget.dedupPairsPerScan).
            "maxPairsPerScan": 10,
            # Retrieval floor for LLM judging. The daemon score is rank-bounded, not a
            # similarity (see wiki_dedup_judge.py), so it may only narrow the candidate
            # set -- the verdict comes from the judge.
            "candidateMinScore": 0.3,
            "judge": {
                "enabled": True,
                # 중복 판정 엔진을 **신규 후보를 만든** 엔진과 분리한다(CROSS_ENGINE_MODES).
                # 기존 카드 두 장을 비교하는 retroactive scan은 생산자를 모르므로 이 값과
                # 무관하게 예전 순서(host engine → 풀)를 그대로 쓴다.
                "crossEngine": "prefer",
                "timeout": 120,
                "cooldownSeconds": 600,
                "maxCharsPerPage": 6000,
            },
        },
        "verify": {
            "enabled": True,
            "timeout": 120,
            "onFail": "delete",
            "onInconclusive": "delete",
            # 검수 엔진 분리(VERIFY_CROSS_ENGINE 참고). builtins가 비면 검수 후보 엔진을
            # compile.extractor 풀에서 물려받는다 — adapter argv 해석은 여전히 한 벌
            # (wiki_compile_worker.resolve_extractor_argv)이다.
            "crossEngine": "prefer",
            "builtins": [],
            "cooldownSeconds": 600,
        },
    },
}

# injectSummaryMaxChars 상한. 이 값은 카드 파일 읽기 창까지 키우므로(recall의
# wiki_card_read_limit) 상한 없이 두면 사용자 설정이 blocking hook의 I/O·CPU 예산을
# 무제한 확대한다. 4000은 관측된 최장 카드 본문(1574자)의 2.5배이고, topN 기본 3에서
# 주입 본문 최악 12000자(읽기 창 10048자/장)로 유계다.
MAX_INJECT_SUMMARY_CHARS = 4000

# injectSourcePathsPerCard 상한. 이 값이 카드당 stat() 호출 수이자 주입 줄 수의 상한이므로
# (topN × 이 값) blocking hook 예산을 유계로 둔다. 관측 최대 소스 수는 4다.
MAX_INJECT_SOURCE_PATHS_PER_CARD = 10

# sourceScan.maxCardsPerScan 상한. 유료 호출은 없지만(stat뿐) 이 값이 update worker
# 한 회차의 파일 I/O를 정하므로 설정 오류가 예산을 무제한 늘리지 못하게 클램프한다.
# 5000은 실측 117µs/장 × 5000 ≈ 0.6초이고 라이브 최대 코퍼스(931장)의 5배다. 창이
# 코퍼스보다 크면 `rotate`가 전량을 돌려주므로 이 상한은 "전량 스캔"을 막지 않는다.
MAX_SOURCE_SCAN_CARDS = 5000

# 유료 host CLI 호출 수를 정하는 값들의 **상한 클램프**. 이 값들은 "설정 오류나 모델 출력이
# 과금 규모를 정하지 못한다"를 보장하는 자리이므로 설정으로 다시 열지 않는다.
#   - MAX_COMPILE_PER_RUN: `compile.batch.maxPerRun`(= run당 extractor 호출 수). 예전에는
#     `99999999`도 그대로 통과해 한 run이 수만 회 호출을 시도할 수 있었다.
#   - MAX_CARDS_PER_SOURCE: `compile.maxCardsPerSource`(= 소스당 카드 수, 모델 출력 상한).
#   - MAX_VERIFY_PER_RUN: `compile.verify.maxPerRun`(사람이 명시한 검수 예산).
#   - VERIFY_PRODUCED_HARD_CAP: **모델 출력에서 유도된** 검수 예산의 상한. verify 예산은
#     `max(verify.maxPerRun, min(생산량, 이 값))`이라 사람이 적은 값은 언제나 존중되고
#     모델이 만든 카드 수는 이 상한을 넘겨 과금을 늘릴 수 없다. 30은 기본 배치
#     (maxPerRun 10 × 실측 카드 증폭 2.68 ≈ 27)를 덮어 정상 흐름의 즉시 검수를 유지하면서
#     퇴화한 run(카드 200장)의 호출 폭발을 막는 값이다.
MAX_COMPILE_PER_RUN = 50
MAX_CARDS_PER_SOURCE = 50
MAX_VERIFY_PER_RUN = 50
VERIFY_PRODUCED_HARD_CAP = 30

COMPILE_MODES = {"off", "candidates", "guarded", "auto-wiki"}
WIKI_STATUSES = {"generated", "verified", "tentative", "contested", "discarded", "superseded"}
# `compile.defaultStatus`가 받는 값. `WIKI_STATUSES` 전체가 아니다 — `verified`를
# 넣으면 기계 검수를 거치지 않은 신규 카드가 `recallVerifiedOnly` 기본값 아래에서 곧바로
# 캐논 근거로 인용된다(검수 파이프라인 전체를 설정 한 줄로 우회하는 구멍이었다).
COMPILE_DEFAULT_STATUSES = {"generated", "tentative"}

# --- dedup score 레버 (설정에서 상수로) ---------------------------------------
# daemon score는 유사도가 아니라 **RRF 순위의 함수**라(rerank 경로도 blendedScore가 순위를
# 섞는다 — wiki_dedup_judge 모듈 docstring의 실측 참조) 어떤 임계도 "두 카드가 같은 사실을
# 말한다"를 표현할 수 없다. 판정은 LLM judge가 하고 score는 후보 retrieval floor
# (`semanticDedup.candidateMinScore`)로만 남는다. 아래 넷은 judge가 **없는** 머신의 레거시
# 무료 게이트 동작을 예전 기본값 그대로 동결한 것이다 — 설정으로 다시 열지 않는다(도달
# 불가능한 값을 사용자가 조정하게 두는 것이 이 정리의 제거 대상이었다).
DEDUP_SCORE_THRESHOLD = 0.82        # ← semanticDedup.threshold (write-time 레거시 게이트)
DEDUP_AUTO_MERGE_THRESHOLD = 0.9    # ← semanticDedup.autoMergeThreshold (scan 레거시 게이트)
DEDUP_TOP_K = 3                     # ← semanticDedup.topK (유사 카드 retrieval 개수)
DEDUP_SIMILAR_PAGE_MAX_CHARS = 12000  # ← semanticDedup.similarPageMaxChars
# onFail과 onInconclusive가 공유하는 값 집합. "none"이 유일한 "현행 유지"(카드를
# generated로 남김) 표현이므로, 기본값을 delete로 올려도 기존 동작을 명시 선택할 수 있다.
VERIFY_ON_FAIL = {"delete", "contested", "none"}
# 기계 검수 엔진을 카드를 만든 엔진과 분리하는 정책. LLM 자기 검수는 성능을 떨어뜨리고
# (Huang et al. ICLR'24) 문헌이 제시한 완화법은 생성 모델과 판정 모델의 분리다.
#   prefer  — 다른 엔진을 먼저 시도하고, 없으면 같은 엔진으로 검증(자기검증으로 기록)
#   require — 다른 엔진만 허용. 없으면 검증하지 않고 큐를 보존한다(카드는 generated로
#             남아 recallVerifiedOnly 기본값 아래 recall에서 빠진다 — 단일 CLI 머신에서는
#             wiki가 통째로 사라지므로 명시 선택만 허용한다)
#   off     — 0.x 동작(카드를 만든 엔진으로 검증)
VERIFY_CROSS_ENGINE = {"prefer", "require", "off"}
# dedup judge(`compile.semanticDedup.judge.crossEngine`)가 **같은 어휘**를 쓴다. 값 집합을
# 따로 두면 두 파이프라인의 정책 이름이 갈려 사용자가 한쪽만 끄게 된다. 뜻도 같다 —
# 다만 dedup에서 `require`가 만족되지 않으면 잡 보존이 아니라 `unavailable`(레거시 score
# 게이트로 degrade)이다: 판정 대상이 큐가 아니라 지금 쓰려는 카드이므로 보류할 자리가 없다.
CROSS_ENGINE_MODES = VERIFY_CROSS_ENGINE
# 카드 frontmatter `verifiedMode` = 검수 엔진과 생성 엔진의 관계. 값 집합은 코드가 정하고
# 모델 입력이 아니다. 이 필드가 **없는** 카드는 4단계 이전에 검수된 카드로, 자기검증일
# 가능성이 높다(실측 655/688) — 없음은 cross-engine을 뜻하지 않는다.
VERIFIED_MODE_CROSS = "cross-engine"
VERIFIED_MODE_SELF = "self"
VERIFIED_MODE_UNKNOWN = "unknown"
VERIFIED_MODES = {VERIFIED_MODE_CROSS, VERIFIED_MODE_SELF, VERIFIED_MODE_UNKNOWN}
# status: verified/contested와 **함께** 써야 하는 증명 필드. 리셋 시 함께 제거된다.
# 검증 증명 필드. 리셋 경로(`wiki_compile` frontmatter merge)가 이 튜플을 보고
# **키째 제거**하므로, 취소된 검증의
# 잔재가 남지 않는다. `verifiedBodyHash` 도 증명이다 — 남으면 새 본문에 옛
# 지문이 붙어 영구 불일치가 된다.
VERIFY_PROOF_FIELDS = ("verifiedBy", "verifiedAt", "verifiedMode", "verifiedBodyHash")
# 생성 엔진을 알 수 없을 때 큐/레코드에 쓰는 sentinel. **리터럴을 각자 들고 있으면 안 된다** —
# `wiki_verify_worker`가 "귀속 불가"를 이 값으로 판정하므로(교차검증 주장 금지) 생산 측
# (`wiki_compile_enqueue`·`sync`·`wiki_compile_worker`)과 같은 문자열이어야 한다. 갈려 있던
# 동안 planner의 빈 문자열 가드가 사실상 죽은 분기였다.
UNKNOWN_ENGINE = "unknown"
COMPILE_TRIGGERS = {
    "explicit_user_approval",
    "post_session_summary",
    "post_tool_source",
    # 수동 sync(core/sync.py)가 스냅샷 diff로 찾아낸 소스 변경. git pull·rebase·외부
    # 편집은 PostToolUse 훅을 타지 않으므로 `post_tool_source`로는 잡히지 않는다.
    "post_sync_source",
    # 별도 동의가 있을 때만 SessionStart가 새 소스를 위키 컴파일 큐에 넣는다.
    "session_start_source",
    "repeated_recall",
    "cross_file_conclusion",
    "manual",
}
BUILTIN_EXTRACTOR_ENGINES = {"claude", "codex", "hermes"}
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
REASONING_EFFORT_PHASES = ("generation", "verify", "semanticDedup")
DEFAULT_REASONING_EFFORT = {
    "generation": "low",
    "verify": "medium",
    "semanticDedup": "medium",
    "engines": {},
}

# ---------------------------------------------------------------------------
# Minimal settings model
#
# 프로젝트 settings가 `name`(필수)과 `skipPaths`(선택)만 가지면 나머지 effective 설정은
# 플러그인이 결정적으로 유도한다. 다른 키가 하나라도 있으면 legacy 파일이며 지금까지와
# 똑같이 정규화한다(부분 유도 없음). 이미 선택된 laya-db route는 settings 바이트 해시에
# 묶여 있으므로 legacy 파일의 해석은 이 모델 때문에 바뀌지 않는다.
MINIMAL_SETTINGS_KEYS = frozenset({"name", "skipPaths"})
MINIMAL_SETTINGS_MODEL = "minimal"
MINIMAL_RAW_COLLECTION_PATH = "."
MINIMAL_WIKI_COLLECTION_PATH = ".auto-context/wiki"
MINIMAL_WIKI_SUFFIX = "-wiki"
# laya_index_host_route.COLLECTION_NAME 상한(64)에서 "-wiki" 접미를 뺀 길이.
MINIMAL_SLUG_MAX = 64 - len(MINIMAL_WIKI_SUFFIX)
MINIMAL_COMPILE_TRIGGERS = ("post_tool_source", "post_sync_source", "manual")
MINIMAL_EXTRACTOR_BUILTINS = ("claude", "codex", "hermes")


def settings_slug(name):
    """`name` → collection 이름 접두. 영숫자 시작, `[a-z0-9._-]`, 59자 이하. 실패 시 ""."""
    if not isinstance(name, str):
        return ""
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "-"
                      for ch in name.strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-._")[:MINIMAL_SLUG_MAX].rstrip("-._")
    return cleaned


def _minimal_skip_paths(value):
    """유효한 skipPaths(비어 있지 않은 문자열 목록)면 복사본, 아니면 None."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return list(value)


def is_minimal_settings(raw):
    """`{name, skipPaths?}`만 가진 유효한 settings인가.

    `name`이 없거나 slug가 비거나 `skipPaths`가 비어 있지 않은 문자열 목록이 아니면
    minimal이 아니다 — legacy 정규화로 떨어져 collection이 비고(pending) 아무것도
    인덱싱하지 않는다(fail-closed).
    """
    return (isinstance(raw, dict) and "name" in raw
            and set(raw) <= MINIMAL_SETTINGS_KEYS
            and bool(settings_slug(raw.get("name")))
            and _minimal_skip_paths(raw.get("skipPaths")) is not None)


def minimal_collection_names(name):
    """(raw collection, wiki collection) 이름. slug가 비면 ValueError."""
    slug = settings_slug(name)
    if not slug:
        raise ValueError("minimal_settings_name_invalid")
    return slug, slug + MINIMAL_WIKI_SUFFIX


def expand_minimal_settings(raw):
    """minimal settings를 같은 의미의 legacy 모양 dict로 펼친다. 아니면 raw 그대로 반환.

    펼친 결과는 legacy 소비자(resolve_paths, update.sh `--raw`, laya route 검사)가 읽는
    모양이다. 프로젝트 루트 전체가 raw(wiki 생성 원천), `.auto-context/wiki`가 wiki이고,
    검색은 wiki 전용, compile은 host builtin extractor의 auto-wiki, 학습은 켜짐이다.
    indexing은 settings 파일이 있으면 켜짐이다.
    """
    if not is_minimal_settings(raw):
        return raw
    name = raw["name"]
    raw_name, wiki_name = minimal_collection_names(name)
    return {
        "name": name,
        "indexing": True,
        "collections": [raw_name, wiki_name],
        "collectionPaths": {raw_name: MINIMAL_RAW_COLLECTION_PATH,
                            wiki_name: MINIMAL_WIKI_COLLECTION_PATH},
        "collectionRoles": {raw_name: COLLECTION_ROLE_RAW, wiki_name: COLLECTION_ROLE_WIKI},
        "skipPaths": _minimal_skip_paths(raw.get("skipPaths")),
        "recallStrategy": "wikiOnly",
        "compile": {
            "mode": "auto-wiki",
            "triggers": list(MINIMAL_COMPILE_TRIGGERS),
            "extractor": {"builtins": list(MINIMAL_EXTRACTOR_BUILTINS)},
        },
        "contextLearning": {"capture": True, "autoCycle": True},
    }


def is_minimal_config(config):
    """정규화된 effective config가 minimal settings에서 유도됐는가."""
    return isinstance(config, dict) and config.get("settingsModel") == MINIMAL_SETTINGS_MODEL


def generation_skip_paths(config, selected_route=None):
    """wiki 생성에서 제외할 경로 목록. 적용 대상이 아니면 None.

    선택된 laya route는 route가 묶은 skipPaths를, minimal settings는 자기 skipPaths를 쓴다.
    legacy QMD 경로의 skipPaths는 recall 결과 필터일 뿐 생성 제외가 아니다.
    """
    if selected_route is not None:
        return selected_route[0]["recallSettings"]["skipPaths"]
    if is_minimal_config(config):
        return list(config.get("skipPaths") or [])
    return None


EVENT_ALIASES = {
    "SessionStart": "sessionStart",
    "session_start": "sessionStart",
    "UserPromptSubmit": "userPromptSubmit",
    "BeforeAgent": "userPromptSubmit",
    "user_prompt_submit": "userPromptSubmit",
    "PostToolUse": "postToolUse",
    "AfterTool": "postToolUse",
    "post_tool_use": "postToolUse",
}


def coerce_float(value, default):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def coerce_int(value, default):
    """양의 정수만 받는다. 실패는 전부 default 다.

    `int(value)` 를 그대로 쓰면 두 가지가 조용히 통과했다(교차 엔진 리뷰 지적):
    `int(True) == 1`(bool 은 int 의 서브클래스), `int(1.9) == 1`(내림). 그래서
    `maxCardsPerScan: 1.9` 를 적은 사용자가 창 1장을 받았다 — 300 장을 기대한 값이
    가장 좁은 값으로 뒤집히는 방향이라 조용한 축소다. bool 과 비정수 실수는 거부하고
    default 로 보낸다(0·음수·비수치와 같은 취급). 숫자 문자열(`"50"`)은 계속 받는다 —
    JSON 에서 실수로 문자열이 되는 일이 흔하고 값 자체는 온전하다.

    **이 함수는 공용이다**(`budget.*` 전 키 + `maxCardsPerScan` 등). 여기만 좁히면
    호출부가 갈리므로 규칙은 이 한 곳에 둔다.
    """
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    # 비정수 실수는 거부한다. `3.0` 같은 정수값 실수는 받는다(JSON number 는 실수로 온다).
    if isinstance(value, float) and result != value:
        return default
    return result if result > 0 else default


def coerce_nonneg_int(value, default, maximum=None):
    """coerce_int와 달리 0을 유효값으로 받고, maximum으로 clamp한다.

    0이 "기능 끄기"의 의미를 갖는 필드(injectSummaryMaxChars)에 쓴다 — coerce_int는
    `result > 0`만 통과시켜 0을 기본값으로 되돌리므로 끌 수 없다.
    bool을 거부하는 이유: JSON `"injectSummaryMaxChars": true`는 흔한 오타이고
    `int(True) == 1`이라 본문이 1자로 잘린 채 조용히 동작한다.
    maximum이 필요한 이유: 이 값이 카드 파일 읽기 창까지 키우므로(recall.wiki_card_read_limit)
    사용자 설정이 blocking hook의 I/O·CPU 예산을 무제한으로 늘릴 수 있다.
    """
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if result < 0:
        return default
    if maximum is not None and result > maximum:
        return maximum
    return result


def coerce_capped_int(value, default, maximum):
    """1 이상 maximum 이하로 강제한다(예산을 정하는 필드 전용).

    대부분은 유료 host CLI 호출 수지만 유료 전용은 아니다 — `sourceScan.maxCardsPerScan`
    처럼 worker 한 회차의 파일 I/O 예산도 같은 규칙으로 클램프한다.

    `coerce_int`는 양수면 무엇이든 통과시키므로 `99999999`가 그대로 run당 호출 상한이
    됐다. 문자열(`"100"`)도 `int()`로 통과하는데 문제는 형이 아니라 **크기**다.
    """
    result = coerce_int(value, default)
    if result > maximum:
        return maximum
    return result


def string_list(value, default=None):
    if default is None:
        default = []
    if not isinstance(value, list):
        return list(default)
    return [item for item in value if isinstance(item, str)]


def string_map(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def extractor_backends(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str)
        and isinstance(item, list)
        and item
        and all(isinstance(arg, str) for arg in item)
    }


def builtin_extractor_engines(value):
    return [
        item
        for item in string_list(value, [])
        if item in BUILTIN_EXTRACTOR_ENGINES
    ]


def reasoning_effort_config(value, defaults=None):
    """Normalize portable phase effort without introducing model configuration."""
    base = defaults if isinstance(defaults, dict) else DEFAULT_REASONING_EFFORT
    result = {
        phase: base.get(phase, DEFAULT_REASONING_EFFORT[phase])
        if base.get(phase) in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT[phase]
        for phase in REASONING_EFFORT_PHASES
    }
    result["engines"] = {}
    raw = value if isinstance(value, dict) else {}
    for phase in REASONING_EFFORT_PHASES:
        if raw.get(phase) in REASONING_EFFORTS:
            result[phase] = raw[phase]
    engines = raw.get("engines") if isinstance(raw.get("engines"), dict) else {}
    for engine, policy in engines.items():
        if not isinstance(engine, str) or not engine or not isinstance(policy, dict):
            continue
        normalized = {
            phase: policy[phase]
            for phase in REASONING_EFFORT_PHASES
            if policy.get(phase) in REASONING_EFFORTS
        }
        if normalized:
            result["engines"][engine] = normalized
    return result


def resolve_reasoning_effort(compile_cfg, engine, phase):
    """Resolve engine/phase policy; engine names remain symbolic and portable."""
    policy = compile_cfg.get("reasoningEffort") if isinstance(compile_cfg, dict) else None
    normalized = reasoning_effort_config(policy)
    phase = phase if phase in REASONING_EFFORT_PHASES else "verify"
    engine_policy = normalized["engines"].get(engine)
    if isinstance(engine_policy, dict) and engine_policy.get(phase) in REASONING_EFFORTS:
        return engine_policy[phase]
    return normalized[phase]


def reasoning_effort_audit(raw, requested=None, reason="capability_not_declared", capability_declared=True):
    """Return bounded, source-free effort audit metadata for persisted records."""
    requested = requested if requested in REASONING_EFFORTS else None
    if not capability_declared:
        return {"requested": requested, "applied": None, "status": "unsupported", "reason": str(reason)[:120]}
    value = raw if isinstance(raw, dict) else {}
    if value.get("requested") in REASONING_EFFORTS:
        requested = value["requested"]
    applied = value.get("applied") if value.get("applied") in REASONING_EFFORTS else None
    status = value.get("status") if value.get("status") in {"applied", "unsupported", "retried_without_effort"} else "unsupported"
    if value.get("reason"):
        reason = str(value["reason"])[:120]
    return {"requested": requested, "applied": applied, "status": status, "reason": str(reason)[:120]}


def collection_role_map(value, collections):
    """normalize된 `collectionRoles`. **미지 값은 버리지 않고 `invalid`로 정규화한다.**

    버리면 normalize 소비자(`load_project_config`를 쓰는 recall·index_enqueue 등)에서
    그 항목이 "키 없음"으로 보여 `raw`로 되살아난다 — raw settings를 직접 읽는
    소비자(`resolve_paths`)와 판정이 갈리고, 하필 갈리는 방향이 fail-open이다.
    센티널을 남기면 두 입력이 같은 답을 낸다.
    """
    if not isinstance(value, dict):
        return {}
    allowed_collections = set(collections)
    normalized = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in allowed_collections:
            continue
        if isinstance(item, str) and item in COLLECTION_ROLES:
            normalized[key] = item
        else:
            normalized[key] = COLLECTION_ROLE_INVALID
    return normalized


def invalid_role_collections(value, collections):
    """`collectionRoles`에서 **role 값이 미지**라 fail-closed된 collection 이름 목록.

    그 컬렉션은 인덱싱·recall·compile에서 전부 빠지므로(값을 못 읽었으니 의도대로
    동작시킬 수 없다) 그 사실이 사용자에게 닿아야 한다. 이 함수의 결과가
    SessionStart notice의 입력이다(`core/update.sh`) — fail-closed의 **종점**이고,
    없으면 색인 안 되는 상태가 조용히 지속된다. collection에 없는 키·비문자열 키는
    role 오타가 아니라 무관한 항목이므로 세지 않는다.

    raw settings(`"sourse"`)와 normalize된 config(`"invalid"` 센티널) 양쪽에서 같은
    답을 낸다 — 센티널도 `COLLECTION_ROLES` 밖이라 자동으로 걸린다.
    """
    if not isinstance(value, dict):
        return []
    allowed_collections = set(collections)
    return sorted(
        key
        for key, item in value.items()
        if isinstance(key, str)
        and key in allowed_collections
        and (not isinstance(item, str) or item not in COLLECTION_ROLES)
    )


def invalid_compile_flags(compile_cfg):
    """compile에서 **값이 present인데 읽을 수 없어** 기본값으로 폴백한 스위치의 키 목록.

    `invalid_role_collections`와 같은 자리·같은 이유다 — "키 없음"(의도 없음)과
    "키 있음 + 값 미지"(의도했는데 못 읽음)를 구분하고, 후자는 fail-open이든
    fail-closed든 **조용하면 안 된다**. 이 함수의 결과가 SessionStart notice의
    입력이고(`core/update.sh`) 그것이 종점이다. raw settings를 받는다 —
    정규화된 config는 이미 bool로 접혀 있어 판정 정보가 없다.

    **범위는 `compile.sourceScan`뿐이다**(블록 자체 + 그 안의 `enabled`). compile의
    다른 스위치도 값 미지면 조용히 기본값으로 떨어지지만 여기서 잡지 않는다:
    `compile.enabled`/`autoWrite`는 `DEPRECATED_FOLDED_KEYS` 알림이, 제거된 키는
    `DEPRECATED_REMOVED_KEYS` 알림이 부분적으로 덮는다(그 알림들은 "키가 있다"만
    보고 값은 보지 않으므로 완전한 커버리지가 아니다 — 넓히는 것은 별건이다).
    `sourceScan.maxCardsPerScan`은 **의도적으로** 제외한다: 그 값의 오류는 "창 폭이
    기본값"으로 끝나고 순환 커서가 커버리지를 유지하지만, `enabled`는 사용자가 적어 둔
    **opt-out 자체가 무력화**되는 자리다. 방향이 다르므로 같은 알림에 묶지 않는다.
    """
    if not isinstance(compile_cfg, dict):
        return []
    if "sourceScan" not in compile_cfg:
        return []
    scan = compile_cfg.get("sourceScan")
    if not isinstance(scan, dict):
        # 블록 자체를 못 읽었다(`sourceScan: "off"`) — 그 안의 모든 값이 기본값이 된다.
        return ["compile.sourceScan"]
    if "enabled" in scan and not isinstance(scan.get("enabled"), bool):
        return ["compile.sourceScan.enabled"]
    return []


def collection_role(roles, collection):
    """collection의 유효 role.

    **키 없음 → `raw`**(role 도입 전 동작, 하위호환 fail-open).
    **키 있음 + 값 미지 → `invalid`**(fail-closed 센티널 — 어떤 role 집합에도 속하지
    않아 인덱싱·recall·compile에서 전부 빠진다). 둘을 구분하는 근거는
    `COLLECTION_ROLE_INVALID` 선언부에 있다.

    **역할 판정은 반드시 이 함수(또는 아래 헬퍼)를 거친다.** 예전에는 여러 곳이
    `roles.get(c) != "wiki"`로 raw를 wiki의 **여집합**으로 정의했는데, `source` 같은
    세 번째 값이 들어오면 그 지점 전부가 오분류한다(인덱싱 안 되는 컬렉션을 recall
    질의에 넣는 등). 여집합을 쓰지 말고 양성 집합으로 판정한다.
    """
    if not isinstance(roles, dict):
        return DEFAULT_COLLECTION_ROLE
    if collection not in roles:
        return DEFAULT_COLLECTION_ROLE
    value = roles.get(collection)
    if isinstance(value, str) and value in COLLECTION_ROLES:
        return value
    return COLLECTION_ROLE_INVALID


def role_map(config):
    """config에서 `collectionRoles`를 안전하게 꺼낸다(dict가 아니면 빈 dict)."""
    if not isinstance(config, dict):
        return {}
    value = config.get("collectionRoles")
    return value if isinstance(value, dict) else {}


def is_wiki_collection(roles, collection):
    return collection_role(roles, collection) == COLLECTION_ROLE_WIKI


def is_indexed_collection(roles, collection):
    return collection_role(roles, collection) in INDEXED_ROLES


def is_compile_source_collection(roles, collection):
    return collection_role(roles, collection) in COMPILE_SOURCE_ROLES


def wiki_collections(collections, roles):
    return [c for c in collections if isinstance(c, str) and is_wiki_collection(roles, c)]


def indexed_collections(collections, roles):
    """qmd에 등록·질의되는 collection만. `source` role은 여기서 빠진다."""
    return [c for c in collections if isinstance(c, str) and is_indexed_collection(roles, c)]


def recall_raw_collections(collections, roles):
    """hierarchical raw backfill 대상 = **인덱싱되는 non-wiki** collection.

    `!= "wiki"` 여집합을 쓰면 안 되는 대표 지점이다 — `source`는 qmd에 등록조차
    되지 않으므로 backfill 질의에 넣으면 그 컬렉션 이름이 데몬에서 무의미해진다.
    """
    return [
        c for c in collections
        if isinstance(c, str) and collection_role(roles, c) in RECALL_RAW_ROLES
    ]


def compile_mode(compile_cfg):
    """정규화된 compile 블록의 유효 mode. 미지 값은 `off`(fail-closed)."""
    if not isinstance(compile_cfg, dict):
        return "off"
    mode = compile_cfg.get("mode")
    return mode if mode in COMPILE_MODES else "off"


def compile_active(compile_cfg):
    """compile 파이프라인(enqueue·worker·write·verify·dedup·notice)이 도는가.

    **판정의 SSOT다.** 예전에는 `enabled` + `mode`를 각 게이트가 제각기 조합했고
    (`wiki_dedup_scan`·`update.sh`는 `enabled`만 봤다) 그 비대칭이 "enabled인데 mode off"
    라는 관측 불가능한 반쯤 켜진 상태를 만들었다. 스위치는 하나이고 판정도 하나다.
    """
    return compile_mode(compile_cfg) != "off"


def compile_config(value):
    if not isinstance(value, dict):
        value = {}
    defaults = DEFAULT_CONFIG["compile"]
    result = dict(defaults)
    result["mode"] = value.get("mode") if value.get("mode") in COMPILE_MODES else defaults["mode"]
    # 제거된 off 스위치는 **끄는 방향으로만** 존중한다(켜는 방향은 무시).
    #
    # `enabled`/`autoWrite`는 mode로 접혀 스키마에서 사라졌고, 나머지 제거 키처럼 무시하는
    # 것이 일관돼 보인다. 그러나 이 둘의 실패 방향은 비대칭이다: `enabled:true`를 무시하면
    # 카드가 안 생길 뿐이지만(복구 가능), **`enabled:false`를 무시하면 사용자가 기록해 둔
    # opt-out을 어기고 유료 host CLI가 돈다**(사용자 계정 청구, 복구 불가). 0.26.0의
    # docs/settings.md가 `enabled:false`를 문서화된 off 스위치로 안내했으므로, 그 형태로
    # 꺼 둔 프로젝트가 플러그인 업그레이드만으로 조용히 재무장하면 안 된다.
    # 알림(SessionStart deprecated-keys)은 이 구멍을 못 덮는다 — TTL 4h 1줄이고 Hermes는
    # notice 채널이 아예 없는데 편집 훅 enqueue는 돈다.
    #
    # 그래서 "죽은 키는 무시한다"의 예외는 딱 두 개이고 방향도 하나다.
    if value.get("enabled") is False:
        result["mode"] = "off"
    elif value.get("autoWrite") is False and result["mode"] in ("auto-wiki", "guarded"):
        # 구버전 `wiki_compile.py`의 쓰기 분기는 `mode == "candidates" or not autoWrite`라
        # autoWrite:false면 **mode와 무관하게** candidate 큐잉까지만 했다. guarded도 같다 —
        # auto-wiki만 접으면 §2 진리표(guarded + autoWrite:false → candidates)와 갈린다.
        result["mode"] = "candidates"
    result["defaultStatus"] = value.get("defaultStatus") if value.get("defaultStatus") in COMPILE_DEFAULT_STATUSES else defaults["defaultStatus"]
    result["excludeStatusesFromRecall"] = [
        status for status in string_list(value.get("excludeStatusesFromRecall"), defaults["excludeStatusesFromRecall"])
        if status in WIKI_STATUSES
    ]
    result["lowPriorityStatuses"] = [
        status for status in string_list(value.get("lowPriorityStatuses"), defaults["lowPriorityStatuses"])
        if status in {"generated", "tentative", "superseded"}
    ]
    result["recallVerifiedOnly"] = (
        value.get("recallVerifiedOnly")
        if isinstance(value.get("recallVerifiedOnly"), bool)
        else defaults["recallVerifiedOnly"]
    )
    result["triggers"] = [
        trigger for trigger in string_list(value.get("triggers"), defaults["triggers"])
        if trigger in COMPILE_TRIGGERS
    ]
    result["maxAutoPageLines"] = coerce_int(value.get("maxAutoPageLines", defaults["maxAutoPageLines"]), defaults["maxAutoPageLines"])
    result["maxSourceChars"] = coerce_int(value.get("maxSourceChars", defaults["maxSourceChars"]), defaults["maxSourceChars"])
    # sourceScan은 `wiki_lifecycle.run`이 `enabled`·`maxCardsPerScan`을 읽는다.
    #
    # `enabled`는 bool만 받는다. **비-bool이면 기본값(True = 수명 주기 유지)으로 폴백하되
    # 침묵하지 않는다** — 폴백만 하면 사용자가 적어 둔 opt-out이 무력화되는데(`"false"`
    # 문자열도 결과가 "껐는데 돈다"다) 그 사실이 어디에도 나타나지 않는다. 종점은
    # `invalid_compile_flags` → SessionStart `notice_once`이고, `COLLECTION_ROLE_INVALID`
    # 와 같은 규칙이다("키 없음"과 "키 있음 + 값 미지"를 구분하고 후자는 조용하면 안 된다).
    # **끄는 쪽으로 읽지 않는 이유**: 수명 주기는 제품 기본 동작이고, 비-bool 집합에는
    # 켜려는 의도의 값(`1`·`"true"`·`"yes"`)도 들어 있다. 켜진 쪽의 삭제는 보관본
    # (`retired-wiki/`)으로 복구할 수 있고 대량 삭제 가드가 한 run의 폭을 막는다.
    raw_scan = value.get("sourceScan")
    scan = raw_scan if isinstance(raw_scan, dict) else {}
    default_scan = defaults["sourceScan"]
    result["sourceScan"] = {
        "enabled": (
            scan.get("enabled")
            if isinstance(scan.get("enabled"), bool)
            else default_scan["enabled"]
        ),
        "maxCardsPerScan": coerce_capped_int(
            scan.get("maxCardsPerScan", default_scan["maxCardsPerScan"]),
            default_scan["maxCardsPerScan"], MAX_SOURCE_SCAN_CARDS,
        ),
    }
    result["reasoningEffort"] = reasoning_effort_config(
        value.get("reasoningEffort"), defaults.get("reasoningEffort")
    )
    raw_extractor = value.get("extractor")
    extractor = raw_extractor if isinstance(raw_extractor, dict) else {}
    default_extractor = defaults["extractor"]
    result["extractor"] = {
        # `dispatch` 게이트가 사라졌으므로 backends/builtins는 **무조건** 해석된다.
        # 게이트가 있던 동안 그 키를 안 적은 config는 엔진이 하나도 해석되지 않아
        # `wiki_compile_worker.process_job`이 잡을 폐기했다(`missing_extractor`).
        "backends": extractor_backends(extractor.get("backends")),
        "builtins": builtin_extractor_engines(extractor.get("builtins")),
        "timeout": coerce_int(extractor.get("timeout", default_extractor["timeout"]), default_extractor["timeout"]),
        "cooldownSeconds": coerce_int(extractor.get("cooldownSeconds", default_extractor["cooldownSeconds"]), default_extractor["cooldownSeconds"]),
    }
    # **상한이 있는 값은 여기서 클램프된다.** 클램프를 budget으로 함께 옮기지 않으면
    # `"100"`·`99999999`가 다시 통과한다(worker 쪽 클램프는 2차 방어다).
    #
    # `dedupPairsPerScan`/`dedupPairsPerCompile`에는 상한이 없다 — 구버전
    # (`judge.maxPairs*`)과 같고, 의도된 상태다. 코드가 자르는 것은 **모델 출력에서 유도된**
    # 값뿐이고(`cardsPerSource`·`VERIFY_PRODUCED_HARD_CAP`), 사람이 명시한 쌍 수는 그 사람의
    # 선택이다. 곱이 커지는 경우는 docs/settings.md의 budget 절이 셈과 함께 경고한다.
    raw_budget = value.get("budget")
    budget = raw_budget if isinstance(raw_budget, dict) else {}
    default_budget = defaults["budget"]
    # **`compile.maxCardsPerSource`만 값을 실제로 이전한다**(`DEPRECATED_RELOCATED_KEYS`의
    # 이름값대로). 새 키가 **키로 존재하면** 새 키가 이기고, 없을 때만 옛 값이 기본값
    # 자리를 대신한 뒤 같은 `coerce_capped_int`를 통과한다 — 이전은 위치 이동이므로
    # 새 키에 그 값을 적은 것과 결과가 같아야 한다. 알림은 그대로 나간다(옮겨 적으라는
    # 안내는 여전히 유효하다 — 잠정 반영이지 새 스키마가 아니다).
    #
    # **같은 목록의 나머지 4키는 이전하지 않는다.** (1) `test/live-settings-freeze.test.mjs`
    # 의 "§7.2 마이그레이션 부채" 테스트가 라이브 3개 파일의 `verifyPerRun=3`을 못박아
    # 2026-08-03 "하위호환 폐기" 결정을 계약으로 동결해 뒀다. (2) 그 4키의 이전 방향은
    # **비용 증가**다(실측: verify 예산 3→15, 3→50) — 플러그인 업그레이드만으로 유료
    # 호출이 늘어나는 방향이라, "끄는 방향으로만 존중한다"는 위 compile off 스위치의
    # 비대칭과 정반대다. (3) `maxCardsPerSource`는 라이브 사용이 0건이라 반영해도 지금
    # 예산이 바뀌는 프로젝트가 없고, 이 키를 적는 동기가 카드 수를 줄이는 것(계획서 재현
    # 사례 `4` < 기본 `10` = 비용 감소 방향)이다.
    legacy_cards_per_source = default_budget["cardsPerSource"]
    if "maxCardsPerSource" in value:
        legacy_cards_per_source = value.get("maxCardsPerSource")
    result["budget"] = {
        "extractorPerRun": coerce_capped_int(
            budget.get("extractorPerRun", default_budget["extractorPerRun"]),
            default_budget["extractorPerRun"], MAX_COMPILE_PER_RUN,
        ),
        "cardsPerSource": coerce_capped_int(
            budget.get("cardsPerSource", legacy_cards_per_source),
            default_budget["cardsPerSource"], MAX_CARDS_PER_SOURCE,
        ),
        "verifyPerRun": coerce_capped_int(
            budget.get("verifyPerRun", default_budget["verifyPerRun"]),
            default_budget["verifyPerRun"], MAX_VERIFY_PER_RUN,
        ),
        "dedupPairsPerScan": coerce_int(
            budget.get("dedupPairsPerScan", default_budget["dedupPairsPerScan"]),
            default_budget["dedupPairsPerScan"],
        ),
        "dedupPairsPerCompile": coerce_int(
            budget.get("dedupPairsPerCompile", default_budget["dedupPairsPerCompile"]),
            default_budget["dedupPairsPerCompile"],
        ),
    }
    raw_batch = value.get("batch")
    batch = raw_batch if isinstance(raw_batch, dict) else {}
    default_batch = defaults["batch"]
    result["batch"] = {
        "idleSeconds": coerce_int(batch.get("idleSeconds", default_batch["idleSeconds"]), default_batch["idleSeconds"]),
        "maxItems": coerce_int(batch.get("maxItems", default_batch["maxItems"]), default_batch["maxItems"]),
    }
    raw_semantic = value.get("semanticDedup")
    semantic = raw_semantic if isinstance(raw_semantic, dict) else {}
    default_semantic = defaults["semanticDedup"]
    raw_judge = semantic.get("judge")
    judge = raw_judge if isinstance(raw_judge, dict) else {}
    default_judge = default_semantic["judge"]
    result["semanticDedup"] = {
        "enabled": semantic.get("enabled") if isinstance(semantic.get("enabled"), bool) else default_semantic["enabled"],
        "maxPairsPerScan": coerce_int(semantic.get("maxPairsPerScan", default_semantic["maxPairsPerScan"]), default_semantic["maxPairsPerScan"]),
        "candidateMinScore": coerce_float(semantic.get("candidateMinScore", default_semantic["candidateMinScore"]), default_semantic["candidateMinScore"]),
        "judge": {
            "enabled": judge.get("enabled") if isinstance(judge.get("enabled"), bool) else default_judge["enabled"],
            "crossEngine": judge.get("crossEngine") if judge.get("crossEngine") in CROSS_ENGINE_MODES else default_judge["crossEngine"],
            "timeout": coerce_int(judge.get("timeout", default_judge["timeout"]), default_judge["timeout"]),
            "cooldownSeconds": coerce_int(judge.get("cooldownSeconds", default_judge["cooldownSeconds"]), default_judge["cooldownSeconds"]),
            "maxCharsPerPage": coerce_int(judge.get("maxCharsPerPage", default_judge["maxCharsPerPage"]), default_judge["maxCharsPerPage"]),
        },
    }
    raw_verify = value.get("verify")
    verify = raw_verify if isinstance(raw_verify, dict) else {}
    default_verify = defaults["verify"]
    result["verify"] = {
        "enabled": verify.get("enabled") if isinstance(verify.get("enabled"), bool) else default_verify["enabled"],
        "timeout": coerce_int(verify.get("timeout", default_verify["timeout"]), default_verify["timeout"]),
        "onFail": verify.get("onFail") if verify.get("onFail") in VERIFY_ON_FAIL else default_verify["onFail"],
        "onInconclusive": verify.get("onInconclusive") if verify.get("onInconclusive") in VERIFY_ON_FAIL else default_verify["onInconclusive"],
        "crossEngine": verify.get("crossEngine") if verify.get("crossEngine") in VERIFY_CROSS_ENGINE else default_verify["crossEngine"],
        # 심볼릭 엔진 이름만 받는다(adapter 경로 금지). 빈 목록 = extractor 풀 상속.
        "builtins": string_list(verify.get("builtins"), default_verify["builtins"]),
        "cooldownSeconds": coerce_int(verify.get("cooldownSeconds", default_verify["cooldownSeconds"]), default_verify["cooldownSeconds"]),
    }
    return result


def maintenance_config(value):
    """qmd 인덱스 유지보수 설정. 사용자 인덱스를 만지는 동작이라 전부 끌 수 있어야 한다.

    임계(minRatio/minCount)는 "이만큼 죽은 벡터가 쌓이면 회수한다"이므로 0도 유효값이다
    (0 = 임계 없음 = 조금이라도 있으면 회수). `coerce_int`는 0을 기본값으로 되돌리므로
    쓸 수 없다 — `coerce_nonneg_int`/직접 검사를 쓴다. cooldownSeconds는 반대로 0이면
    매 세션 vacuum이 붙어 금지 사항이 되므로 양수만 받는다(`coerce_int`).
    """
    defaults = DEFAULT_CONFIG["maintenance"]["orphanVectors"]
    raw = value if isinstance(value, dict) else {}
    orphan = raw.get("orphanVectors") if isinstance(raw.get("orphanVectors"), dict) else {}
    ratio = coerce_float(orphan.get("minRatio", defaults["minRatio"]), defaults["minRatio"])
    if ratio < 0 or ratio > 1:
        ratio = defaults["minRatio"]
    return {
        "orphanVectors": {
            "enabled": (
                orphan.get("enabled")
                if isinstance(orphan.get("enabled"), bool)
                else defaults["enabled"]
            ),
            "minRatio": ratio,
            "minCount": coerce_nonneg_int(orphan.get("minCount", defaults["minCount"]), defaults["minCount"]),
            "cooldownSeconds": coerce_int(
                orphan.get("cooldownSeconds", defaults["cooldownSeconds"]),
                defaults["cooldownSeconds"],
            ),
        },
    }


def has_legacy_novel_collection(collections):
    return any(
        collection.endswith("-manuscript") or collection.endswith("-plot")
        for collection in collections
    )


def canonical_event_name(event_name):
    return EVENT_ALIASES.get(event_name, event_name)


def event_enabled(config, event_name):
    events = config.get("events", DEFAULT_CONFIG["events"])
    if not isinstance(events, list):
        events = DEFAULT_CONFIG["events"]
    return canonical_event_name(event_name) in events


def load_input_config():
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# 제거된 설정 키 감지 (deprecated key notice)
#
# 스키마 정리로 사라진 키는 `normalize_config()`가 **조용히 무시한다**(하위호환 없음).
# 조용한 무시는 제거보다 나쁘다 — `verify.maxPerRun: 15`를 적어 둔 사용자는 그 값이
# 여전히 검수 예산이라고 믿는데 코드는 `budget.verifyPerRun`을 읽는다. 종점은
# SessionStart의 `notice_once` 1줄이다(`core/update.sh`).
#
# **감지는 `normalize_config()` 안에서 하지 않고 이 함수들로 분리한다.** 이유는 비용이
# 아니라 **반환 계약**이다: `normalize_config`는 UserPromptSubmit·PostToolUse·enqueue
# 같은 blocking hook에서 프롬프트마다 돌고 그 반환 dict는 effective config로서
# 동결 테스트(`config-emission-freeze` · `live-settings-freeze`)·`--raw` 출력·
# recall/worker 소비자가 그대로 읽는다. 거기에 `deprecatedKeys`를 얹으면 모든 소비자가
# 무시해야 할 키가 하나 늘고 두 동결 스냅샷이 통째로 흔들린다. 분리해 두면 호출자가
# **update 경로 하나**로 고정되므로, marker 쓰기가 hook 경로로 새지 않는다는 성질도
# 코드 구조로 보장된다(Hermes 등 stdout이 없는 호스트가 marker를 선점하면 이후
# Claude/Codex 세션의 알림이 통째로 삼켜진다 — notice_once의 알려진 함정).
#
# 값이 옮겨간 키. 사용자 값이 여전히 의미를 가지므로 **행선지를 알려준다**.
DEPRECATED_RELOCATED_KEYS = {
    "compile.batch.maxPerRun": "compile.budget.extractorPerRun",
    "compile.maxCardsPerSource": "compile.budget.cardsPerSource",
    "compile.verify.maxPerRun": "compile.budget.verifyPerRun",
    "compile.semanticDedup.judge.maxPairsPerScan": "compile.budget.dedupPairsPerScan",
    "compile.semanticDedup.judge.maxPairsPerCompile": "compile.budget.dedupPairsPerCompile",
}

# `compile.mode`로 흡수된 스위치. "사라졌다"가 아니라 "한 값으로 합쳐졌다"라서 문구가
# 따로다 — 사용자는 지우는 것이 아니라 `mode`를 다시 정해야 한다(진리표는 계획 §2).
DEPRECATED_FOLDED_KEYS = ("compile.enabled", "compile.autoWrite")

# 대체가 없는 키. 경로 9종은 상수화됐고(`core/compile_paths.py`) 위치는 그대로이므로
# 지우기만 하면 된다. dedup 레버 4종은 daemon score가 순위 기반이라 도달 불가였고
# extractor 3종은 backends/builtins로 일원화됐다. 선언 순서가 곧 알림 문구의 순서다
# (알림이 실행마다 흔들리지 않게 — dict/tuple 순회 순서를 그대로 쓴다).
DEPRECATED_REMOVED_KEYS = (
    "compile.canonSignals",
    "compile.requireReviewForCanon",
    "compile.candidatePath",
    "compile.sourceQueuePath",
    "compile.tombstonePath",
    "compile.manifestPath",
    "compile.mergeNeededPath",
    "compile.verify.queuePath",
    "compile.verify.logPath",
    "compile.verify.skippedPath",
    "compile.verify.deletedPath",
    # 원문 소실 원장 경로 키. 소비자가 없으므로 알림 대상이다.
    "compile.sourceMissingPath",
    "compile.extractor.argv",
    "compile.extractor.dispatch",
    "compile.extractor.default",
    "compile.semanticDedup.threshold",
    "compile.semanticDedup.autoMergeThreshold",
    "compile.semanticDedup.topK",
    "compile.semanticDedup.similarPageMaxChars",
)


def _has_key_path(node, dotted):
    """`a.b.c`가 중첩 dict에 **키로** 존재하는가. 값은 보지 않는다(`null`도 존재다)."""
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def deprecated_keys(input_config):
    """raw(정규화 전) config에 남아 있는 제거된 키 목록.

    반환은 `{"key", "kind", "replacement"}` 레코드 목록이고 kind는
    `relocated`(행선지 있음) / `folded`(mode로 흡수) / `removed`(대체 없음)다.
    호출자는 `core/update.sh`의 SessionStart 경로 하나다 — hook에서 부르지 말 것.
    """
    if not isinstance(input_config, dict):
        return []
    found = []
    for key, replacement in DEPRECATED_RELOCATED_KEYS.items():
        if _has_key_path(input_config, key):
            found.append({"key": key, "kind": "relocated", "replacement": replacement})
    for key in DEPRECATED_FOLDED_KEYS:
        if _has_key_path(input_config, key):
            found.append({"key": key, "kind": "folded", "replacement": "compile.mode"})
    for key in DEPRECATED_REMOVED_KEYS:
        if _has_key_path(input_config, key):
            found.append({"key": key, "kind": "removed", "replacement": None})
    return found


def deprecated_key_notice(input_config):
    """SessionStart 알림 1줄. 제거된 키가 없으면 빈 문자열.

    문구를 bash가 아니라 여기서 만드는 것은 형제 알림(role-invalid·dead-registration)과
    다르다 — 저쪽은 python이 숫자/이름만 주고 bash가 문장을 만든다. 여기서는 문장이
    **세 갈래로 갈리고** 각 갈래가 키 목록을 끼고 있어 bash에서 조립하면 세 벌의
    분기가 생기고, 그 분기가 위 세 상수와 갈리는 순간 알림이 틀린 행선지를 말한다.
    """
    records = deprecated_keys(input_config)
    if not records:
        return ""
    parts = []
    relocated = ["%s → %s" % (r["key"], r["replacement"]) for r in records if r["kind"] == "relocated"]
    if relocated:
        parts.append("옮겨짐(값을 새 키로 다시 적으세요): " + ", ".join(relocated))
    folded = [r["key"] for r in records if r["kind"] == "folded"]
    if folded:
        parts.append("compile.mode로 통합(mode 값 하나로 지정하세요): " + ", ".join(folded))
    removed = [r["key"] for r in records if r["kind"] == "removed"]
    if removed:
        parts.append("제거됨(대체 없음, 지우세요): " + ", ".join(removed))
    return (
        "[qmd] .auto-context/settings.json에 더 이상 읽지 않는 설정 키 %d개가 있습니다 — "
        "적어 둔 값은 무시됩니다. %s" % (len(records), " / ".join(parts))
    )


def invalid_session_start_sync(input_config):
    """A present non-boolean opt-out is invalid and must not become default-on."""
    return (isinstance(input_config, dict) and "sessionStartSync" in input_config
            and not isinstance(input_config["sessionStartSync"], bool))


def normalize_config(input_config):
    minimal = is_minimal_settings(input_config)
    if minimal:
        input_config = expand_minimal_settings(input_config)
    config = _normalize_expanded_config(input_config)
    if minimal:
        config["settingsModel"] = MINIMAL_SETTINGS_MODEL
    return config


def _normalize_expanded_config(input_config):
    config = dict(DEFAULT_CONFIG)
    # Missing remains on; explicit bool stays intact; an unreadable value is off.
    raw_sync = input_config.get("sessionStartSync", DEFAULT_CONFIG["sessionStartSync"])
    config["sessionStartSync"] = raw_sync if isinstance(raw_sync, bool) else False
    learning = input_config.get('contextLearning')
    if isinstance(learning, dict):
        config['contextLearning'] = {
            'capture': learning.get('capture') is True,
            'autoCycle': learning.get('autoCycle') is True,
            # The legacy liveSelection key is ignored: selected Laya recall
            # always uses the local selector, independent of project settings.
            'candidateTopK': learning.get('candidateTopK') if type(learning.get('candidateTopK')) is int and 1 <= learning['candidateTopK'] <= 15 else 8,
            'stateRoot': learning.get('stateRoot') if isinstance(learning.get('stateRoot'), str) else None,
        }
        # Omission selects the per-user plugin default. Keep an explicitly
        # invalid value distinguishable so it cannot silently select that root.
        if 'stateRoot' not in learning:
            config['contextLearning'].pop('stateRoot')
    shared = input_config.get('sharedLearning')
    if isinstance(shared, dict):
        rerank_target = shared.get('rerankTargetSeconds', 1.0)
        if (type(rerank_target) not in (int, float)
                or not math.isfinite(rerank_target)
                or not .2 <= rerank_target <= 10):
            rerank_target = 1.0
        config['sharedLearning'] = {
            'enabled': shared.get('enabled') is True,
            'root': shared.get('root') if isinstance(shared.get('root'), str) else None,
            'trialSelection': shared.get('trialSelection') is True,
            'rerankTargetSeconds': rerank_target,
        }
        if shared.get('profile') == 'qmd-local-learning-profile-v1':
            config['sharedLearning']['profile'] = shared['profile']
            config['sharedLearning']['autoApply'] = shared.get('autoApply') is True
        if (config['sharedLearning']['enabled'] and config['sharedLearning']['trialSelection']
                and 'contextLearning' in config):
            config['contextLearning']['candidateTopK'] = 15
    config["name"] = input_config.get("name", DEFAULT_CONFIG["name"]) if isinstance(input_config.get("name", ""), str) else DEFAULT_CONFIG["name"]
    config["collections"] = string_list(input_config.get("collections"), DEFAULT_CONFIG["collections"])
    config["minScore"] = coerce_float(input_config.get("minScore", DEFAULT_CONFIG["minScore"]), DEFAULT_CONFIG["minScore"])
    config["rawFallbackMinScore"] = coerce_float(input_config.get("rawFallbackMinScore", config["minScore"]), config["minScore"])
    config["topN"] = coerce_int(input_config.get("topN", DEFAULT_CONFIG["topN"]), DEFAULT_CONFIG["topN"])
    config["queryTimeout"] = coerce_float(input_config.get("queryTimeout", DEFAULT_CONFIG["queryTimeout"]), DEFAULT_CONFIG["queryTimeout"])
    config["staleQueueThreshold"] = coerce_int(input_config.get("staleQueueThreshold", DEFAULT_CONFIG["staleQueueThreshold"]), DEFAULT_CONFIG["staleQueueThreshold"])
    config["skipPaths"] = string_list(input_config.get("skipPaths"), DEFAULT_CONFIG["skipPaths"])
    config["collectionPaths"] = string_map(input_config.get("collectionPaths"))
    config["allowRoots"] = string_list(input_config.get("allowRoots"), DEFAULT_CONFIG["allowRoots"])
    config["prefixStyle"] = input_config.get("prefixStyle") if input_config.get("prefixStyle") in ("full", "tag") else DEFAULT_CONFIG["prefixStyle"]
    config["injectSummaryMaxChars"] = coerce_nonneg_int(
        input_config.get("injectSummaryMaxChars", DEFAULT_CONFIG["injectSummaryMaxChars"]),
        DEFAULT_CONFIG["injectSummaryMaxChars"],
        MAX_INJECT_SUMMARY_CHARS,
    )
    config["injectSourcePathsPerCard"] = coerce_nonneg_int(
        input_config.get("injectSourcePathsPerCard", DEFAULT_CONFIG["injectSourcePathsPerCard"]),
        DEFAULT_CONFIG["injectSourcePathsPerCard"],
        MAX_INJECT_SOURCE_PATHS_PER_CARD,
    )
    config["collectionRoles"] = collection_role_map(input_config.get("collectionRoles"), config["collections"])
    config["recallStrategy"] = input_config.get("recallStrategy") if input_config.get("recallStrategy") in ("flat", "hierarchical", "wikiOnly") else DEFAULT_CONFIG["recallStrategy"]
    config["wikiPath"] = input_config.get("wikiPath") if isinstance(input_config.get("wikiPath"), str) else DEFAULT_CONFIG["wikiPath"]
    config["compile"] = compile_config(input_config.get("compile"))
    config["maintenance"] = maintenance_config(input_config.get("maintenance"))
    if "events" in input_config and isinstance(input_config.get("events"), list):
        config["events"] = [
            canonical_event_name(event)
            for event in string_list(input_config.get("events"), [])
            if canonical_event_name(event) in DEFAULT_CONFIG["events"]
        ]
    else:
        config["events"] = list(DEFAULT_CONFIG["events"])

    val = input_config.get("indexing")
    if isinstance(val, str):                      # "true"/"false" 문자열만 boolean으로 강제, 그 외는 None
        low = val.strip().lower()
        val = True if low == "true" else (False if low == "false" else None)
    config["indexing"] = val if isinstance(val, bool) else None

    if "lexicalPatterns" in input_config:
        config["lexicalPatterns"] = string_list(input_config.get("lexicalPatterns"), DEFAULT_CONFIG["lexicalPatterns"])
    elif has_legacy_novel_collection(config["collections"]):
        config["lexicalPatterns"] = ["ep"]
    else:
        config["lexicalPatterns"] = list(DEFAULT_CONFIG["lexicalPatterns"])
    return config


def _is_within(path, root):
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False

def _project_search_dirs(cwd):
    path = Path(cwd).resolve()
    home = Path.home().resolve()
    # HOME 자체이거나 HOME 밖이면 cwd만; HOME 하위면 HOME까지만 부모 탐색.
    if path == home or not _is_within(path, home):
        return [path]
    search = [path]
    for parent in path.parents:
        search.append(parent)
        if parent == home:
            break
    return search


def project_identity_root(cwd):
    """Return the repo-level root used for local per-user decisions."""
    path = Path(cwd).resolve()
    home = Path.home().resolve()
    if path == home or not _is_within(path, home):
        return path
    for candidate in _project_search_dirs(path):
        if (candidate / ".git").exists():
            return candidate
    return path


def _candidate_configs(project_dir):
    return [
        (project_dir / CONFIG_DIR_NAME / SETTINGS_FILE_NAME, "auto-context-dir", project_dir),
    ]


def _declared_project_owner(scope):
    """Keep the nearest story config as its search scope; owner is separate."""
    scope = Path(scope).resolve()
    home = Path.home().resolve()
    if scope == home or not _is_within(scope, home):
        return scope
    for parent in scope.parents:
        if parent == home:
            break
        settings = parent / CONFIG_DIR_NAME / SETTINGS_FILE_NAME
        if not settings.is_file() or settings.is_symlink():
            continue
        try:
            raw = json.loads(settings.read_text())
            names = raw.get('ownedScopes') if isinstance(raw, dict) else None
            if (isinstance(names, list) and scope.parent == parent
                    and scope.name in names and all(
                        isinstance(name, str) and name not in ('', '.', '..')
                        and '/' not in name and '\\' not in name for name in names)):
                return parent
        except (OSError, ValueError, TypeError):
            continue
    return scope


def find_project_config(cwd):
    """cwd→부모(HOME 경계)로 project config를 찾고 normalized config와 위치를 반환한다.

    recall/posttool/index_enqueue/wiki_compile_enqueue/preflight_gate 등 여러 hook
    entrypoint가 이 함수를 개별 try/except 없이 안전한 최종 경계로 기대하고 직접
    호출한다. 샌드박스/권한 등 예상 못한 환경 차이로 경로 탐색이 실패해도 여기서
    죽으면 hook 프로세스 자체가 non-zero exit로 죽어 host UI에 hook 실패로
    표면화된다 -- "설정 없음"으로 fail-open해 호출자가 정상 종료하게 한다."""
    try:
        return _find_project_config_unsafe(cwd)
    except Exception:
        try:
            project_root = str(Path(cwd).resolve())
        except Exception:
            project_root = str(cwd)
        fallback = normalize_config({})
        fallback["collections"] = []
        return {
            "config": fallback,
            "configPath": None,
            "configFormat": "none",
            "projectRoot": project_root,
            "projectOwner": project_root,
        }


def _find_project_config_unsafe(cwd):
    path = Path(cwd).resolve()
    config_file = None
    config_format = "none"
    project_root = path
    for d in _project_search_dirs(cwd):
        for cand, fmt, root in _candidate_configs(d):
            if cand.exists():
                config_file = cand
                config_format = fmt
                project_root = root
                break
        if config_file:
            break
    if config_file:
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            config = normalize_config(parsed)
            if config.get("indexing") is False:
                config["collections"] = []
            return {
                "config": config,
                "configPath": str(config_file),
                "configFormat": config_format,
                "projectRoot": str(project_root),
                "projectOwner": str(_declared_project_owner(project_root)),
            }
        except (json.JSONDecodeError, OSError):
            pass
    fallback = normalize_config({})
    fallback["collections"] = []
    return {
        "config": fallback,
        "configPath": None,
        "configFormat": "none",
        "projectRoot": str(path),
        "projectOwner": str(path),
    }


def _read_json_object(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except json.JSONDecodeError:
        return None, "invalid_json"
    except OSError:
        return None, "read_error"
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    return parsed, None


def load_project_config(cwd):
    """Load the effective config from the nearest `.auto-context/settings.json`.
    indexing:false means collections=[] (검색/인덱싱 skip). 못 찾으면 빈 설정(collections=[])."""
    return find_project_config(cwd)["config"]


def load_project_config_raw(cwd):
    """Return the raw JSON object for the discovered project config, or {}.

    minimal settings(`{name, skipPaths}`)는 legacy 모양으로 펼쳐 반환하고
    `settingsModel: "minimal"`을 붙인다(`resolve_paths`가 raw collection을 QMD 인덱스에서
    뺀다). legacy 파일은 읽은 그대로다."""
    found = find_project_config(cwd)
    config_path = found.get("configPath")
    if not config_path:
        return {}
    parsed, reason = _read_json_object(config_path)
    if reason:
        return {}
    if found.get("configFormat") == "auto-context-dir" and is_minimal_settings(parsed):
        return {**expand_minimal_settings(parsed), "settingsModel": MINIMAL_SETTINGS_MODEL}
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Normalize qmd recall configuration.")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--raw", action="store_true", help="Print raw discovered project config JSON instead of stdin normalization.")
    args = parser.parse_args()

    if args.raw:
        print(json.dumps(load_project_config_raw(args.cwd), ensure_ascii=False))
        return

    config = normalize_config(load_input_config())
    print(json.dumps(config, ensure_ascii=False))


if __name__ == "__main__":
    main()
