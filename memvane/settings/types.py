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
            "verify": "low",
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
                # 기본은 신규 후보를 만든 host CLI만 사용한다. 다른 엔진을 쓰는
                # prefer/require 정책은 사용자가 명시적으로 선택할 수 있다.
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
            # 같은 host CLI의 독립 호출로 검수한다. 다른 CLI 설치를 요구하지 않는다.
            # prefer/require를 명시하면 기존 교차 엔진 정책을 사용할 수 있다.
            "crossEngine": "off",
            "builtins": [],
            "cooldownSeconds": 600,
        },
    },
}

DEDUP_AUTO_MERGE_THRESHOLD = 0.9    # ← semanticDedup.autoMergeThreshold (scan cosine 후보 임계)
