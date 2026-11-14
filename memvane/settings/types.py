"""Stable project directory names."""

CURRENT = '.memvane'
NAMES = (CURRENT,)
SETTINGS = 'settings.json'
WIKI = 'wiki'

SOURCE_TEXT_EXTENSIONS = ('.md', '.markdown', '.mdx', '.txt', '.rst', '.adoc')

WIKI_TEXT_EXTENSIONS = ('.md',)

WIKI_META_PAGES = ('SCHEMA.md', 'index.md', 'log.md')

DENIED_SOURCE_SEGMENTS = frozenset({
    '.memvane', '.auto-context', '.git', '.hg', '.svn',
    'node_modules', 'bower_components', 'vendor', 'Pods',
    'venv', 'site-packages', '__pycache__',
    'dist', 'build', 'out', 'target', 'coverage', 'DerivedData',
    'Library', 'Temp', 'obj',  # Unity project caches
})

VERIFIED_MODE_CROSS = "cross-engine"
VERIFIED_MODE_SELF = "self"
VERIFIED_MODE_UNKNOWN = "unknown"
VERIFIED_MODES = {VERIFIED_MODE_CROSS, VERIFIED_MODE_SELF, VERIFIED_MODE_UNKNOWN}


WIKI_REL = CURRENT + '/' + WIKI
COMPILE_REL = CURRENT + '/compile'

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

WIKI_STATUSES = {"generated", "verified", "tentative", "contested", "discarded", "superseded"}

# --- dedup score 레버 (설정에서 상수로) ---------------------------------------
# write-time gate의 retrieval 점수는 유사도가 아니라 순위 hint라 판정은 LLM judge가 하고
# score는 후보 retrieval floor(`semanticDedup.candidateMinScore`)로만 남는다.
# `DEDUP_AUTO_MERGE_THRESHOLD`는 retroactive scan(`wiki_dedup_scan`)의 후보 임계다 — 두 카드의
# 저장된 memvane-db chunk 벡터 중심점 사이 cosine이라 실제 유사도이고, 판정은 resolver 에이전트가
# 한다. 넷 모두 설정으로 열지 않는다.
DEDUP_SCORE_THRESHOLD = 0.82        # ← semanticDedup.threshold (write-time 레거시 게이트)
DEDUP_TOP_K = 20                    # 관련 후보의 상한. 유료 judge 호출 예산과 독립이다.
DEDUP_SIMILAR_PAGE_MAX_CHARS = 12000  # ← semanticDedup.similarPageMaxChars
DEDUP_GROUNDING_MAX_CHARS = 36000    # 렌더된 기존 카드 문맥 전체(헤더/생략 표식 포함)

# 카드 frontmatter `verifiedMode` = 검수 엔진과 생성 엔진의 관계. 값 집합은 코드가 정하고
# 모델 입력이 아니다. 이 필드가 **없는** 카드는 4단계 이전에 검수된 카드로, 자기검증일
# 가능성이 높다(실측 655/688) — 없음은 cross-engine을 뜻하지 않는다.
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

REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


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
    "wikiPath": ".memvane/wiki",
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
        # 신규 카드는 언제나 `generated`다 — 기계 검수만 `verified`로 올린다.
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
            "verify": "low",
            "semanticDedup": "medium",
            "engines": {},
        },
        "extractor": {
            # `backends`는 항상 빈 dict다(엔진 argv는 builtin adapter에서만 온다). 키는
            # 정규화된 config 모양을 유지하려고 남긴다.
            "backends": {},
            # 심볼릭 엔진 이름만 (adapter 경로 금지 — worker가 런타임에 해석한다).
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
            "dedupPairsPerScan": 8,  # judge 호출 상한 키(retroactive scan은 judge를 부르지 않는다)
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
            # retroactive scan(`wiki_dedup_scan`) 1회가 dedup-needed 큐에 넣는 쌍 상한.
            "maxPairsPerScan": 10,
            # Retrieval floor for LLM judging. The daemon score is rank-bounded, not a
            # similarity (see wiki_dedup_judge.py), so it may only narrow the candidate
            # set -- the verdict comes from the judge.
            "candidateMinScore": 0.3,
            "judge": {
                "enabled": True,
                # 신규 후보를 만든 host CLI만 판정한다(다른 값은 없다).
                "crossEngine": "off",
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
            # 같은 host CLI의 독립 호출로 검수한다(다른 값은 없다). 선택된 route는
            # onFail·onInconclusive를 `none`으로 바꿔 카드를 지우지 않는다.
            "crossEngine": "off",
            "builtins": [],
            "cooldownSeconds": 600,
        },
    },
}

DEDUP_AUTO_MERGE_THRESHOLD = 0.9    # ← semanticDedup.autoMergeThreshold (scan cosine 후보 임계)
