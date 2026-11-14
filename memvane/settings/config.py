#!/usr/bin/env python3
import argparse
import json
import math
import sys
from pathlib import Path

from memvane.settings import project_dir
from memvane.settings.types import (DEFAULT_CONFIG, DEDUP_AUTO_MERGE_THRESHOLD,
                                    VERIFIED_MODE_CROSS, VERIFIED_MODE_SELF,
                                    VERIFIED_MODE_UNKNOWN, VERIFIED_MODES,
                                    COLLECTION_ROLE_RAW, COLLECTION_ROLE_WIKI,
                                    COLLECTION_ROLE_SESSION, COLLECTION_ROLE_SOURCE,
                                    COLLECTION_ROLES, COLLECTION_ROLE_INVALID,
                                    DEFAULT_COLLECTION_ROLE, INDEXED_ROLES,
                                    RECALL_RAW_ROLES, COMPILE_SOURCE_ROLES,
                                    MAX_COMPILE_PER_RUN, MAX_CARDS_PER_SOURCE,
                                    MAX_VERIFY_PER_RUN, VERIFY_PRODUCED_HARD_CAP,
                                    WIKI_STATUSES, DEDUP_SCORE_THRESHOLD, DEDUP_TOP_K,
                                    DEDUP_SIMILAR_PAGE_MAX_CHARS,
                                    DEDUP_GROUNDING_MAX_CHARS, VERIFY_PROOF_FIELDS,
                                    UNKNOWN_ENGINE, REASONING_EFFORTS)


# The project folder is `.memvane/` (project_dir).
CONFIG_DIR_NAME = project_dir.CURRENT
SETTINGS_FILE_NAME = "settings.json"

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

COMPILE_MODES = {"off", "candidates", "guarded", "auto-wiki"}


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
# 프로젝트 settings는 name, skipPaths, diagnostics와 학습 프로필 블록
# contextLearning·sharedLearning만 받는다. 나머지 effective 설정은 플러그인이 유도한다.
MINIMAL_SETTINGS_KEYS = frozenset({"name", "skipPaths", "diagnostics",
                                   "contextLearning", "sharedLearning"})
# 학습 블록은 learning_profile이 쓰는 필드만 담는 닫힌 모양이다. 모양이 틀리면 두 블록을
# 모두 버리고 학습 기본값을 쓰며, 정규화된 config의 `settingsNotes`에 사유를 남긴다.
LEARNING_BLOCK_FIELDS = {
    "contextLearning": {"capture": bool, "autoCycle": bool},
    "sharedLearning": {"enabled": bool, "root": str, "profile": str, "autoApply": bool},
}
LEARNING_PROFILE_SCHEMA = "qmd-local-learning-profile-v1"
LEARNING_SETTINGS_INVALID = "learning_settings_invalid"
MINIMAL_CONTEXT_LEARNING = {"capture": True, "autoCycle": True}
MINIMAL_SETTINGS_MODEL = "minimal"
MINIMAL_RAW_COLLECTION_PATH = "."
MINIMAL_WIKI_COLLECTION_PATH = ".memvane/wiki"
MINIMAL_WIKI_SUFFIX = "-wiki"
# laya_index_host_route.COLLECTION_NAME 상한(64)에서 "-wiki" 접미를 뺀 길이.
MINIMAL_SLUG_MAX = 64 - len(MINIMAL_WIKI_SUFFIX)
MINIMAL_COMPILE_TRIGGERS = ("post_tool_source", "post_sync_source", "session_start_source", "manual")
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


def _learning_blocks(raw):
    """minimal settings의 학습 블록 → (검증된 블록 사본, 사유). 블록이 없으면 ({}, None).

    `sharedLearning.root`는 절대 경로, `profile`은 학습 프로필 스키마만 받는다.
    한 블록이라도 틀리면 두 블록 모두 쓰지 않는다.
    """
    blocks = {key: raw[key] for key in LEARNING_BLOCK_FIELDS if key in raw}
    for key, value in blocks.items():
        fields = LEARNING_BLOCK_FIELDS[key]
        if (not isinstance(value, dict) or not set(value) <= set(fields)
                or any(type(item) is not fields[name] for name, item in value.items())):
            return {}, LEARNING_SETTINGS_INVALID
    shared = blocks.get("sharedLearning", {})
    if (("root" in shared and not Path(shared["root"]).is_absolute())
            or ("profile" in shared and shared["profile"] != LEARNING_PROFILE_SCHEMA)):
        return {}, LEARNING_SETTINGS_INVALID
    return {key: dict(value) for key, value in blocks.items()}, None


def is_minimal_settings(raw):
    """name과 선택적인 skipPaths·diagnostics·contextLearning·sharedLearning만 가진 유효한 settings인가.

    `name`이 없거나 slug가 비거나 `skipPaths`가 비어 있지 않은 문자열 목록이 아니면
    minimal이 아니며 프로젝트 설정으로 사용하지 않는다. 학습 블록의 모양은 여기서 보지
    않는다 — 틀린 학습 블록은 학습만 기본값으로 둔다(`_learning_blocks`).
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


def expand_minimal_settings(raw, folder=None):
    """minimal settings를 같은 의미의 legacy 모양 dict로 펼친다. 아니면 raw 그대로 반환.

    펼친 결과는 legacy 소비자(resolve_paths, update.sh `--raw`, laya route 검사)가 읽는
    모양이다. 프로젝트 루트 전체가 raw(wiki 생성 원천), `<folder>/wiki`가 wiki이고,
    검색은 wiki 전용, compile은 host builtin extractor의 auto-wiki, 학습은 켜짐이다.
    유효한 학습 블록은 학습 기본값 위에 적힌 필드를 얹고(`sharedLearning`은 그대로),
    틀린 학습 블록은 버린다. indexing은 settings 파일이 있으면 켜짐이다. `folder`는 프로젝트의 활성 폴더
    이름(`project_dir.active_name`)이고, 생략하면 `.memvane`이다.
    """
    if not is_minimal_settings(raw):
        return raw
    name = raw["name"]
    raw_name, wiki_name = minimal_collection_names(name)
    expanded = {
        "name": name,
        "indexing": True,
        "collections": [raw_name, wiki_name],
        "collectionPaths": {raw_name: MINIMAL_RAW_COLLECTION_PATH,
                            wiki_name: _wiki_rel(folder)},
        "collectionRoles": {raw_name: COLLECTION_ROLE_RAW, wiki_name: COLLECTION_ROLE_WIKI},
        "skipPaths": _minimal_skip_paths(raw.get("skipPaths")),
        "recallStrategy": "wikiOnly",
        "compile": {
            "mode": "auto-wiki",
            "triggers": list(MINIMAL_COMPILE_TRIGGERS),
            "extractor": {"builtins": list(MINIMAL_EXTRACTOR_BUILTINS)},
        },
        "contextLearning": dict(MINIMAL_CONTEXT_LEARNING),
    }
    learning, _ = _learning_blocks(raw)
    expanded["contextLearning"].update(learning.get("contextLearning", {}))
    if "sharedLearning" in learning:
        expanded["sharedLearning"] = learning["sharedLearning"]
    # 진단 동의는 그대로 전달만 한다(해석은 `diagnostics_config`). 키가 없으면 펼친
    # 모양도 예전과 바이트 단위로 같다.
    if "diagnostics" in raw:
        expanded["diagnostics"] = raw["diagnostics"]
    return expanded


def _wiki_rel(folder):
    if folder is None or not project_dir.is_folder_name(folder):
        return MINIMAL_WIKI_COLLECTION_PATH
    return folder + "/" + project_dir.WIKI


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
    """정규화된 compile 블록. 선택지가 있는 값(`defaultStatus`, `extractor.backends`,
    `verify.onFail`·`onInconclusive`·`crossEngine`, `semanticDedup.judge.crossEngine`)은
    settings로 고를 수 없으므로 읽지 않고 기본값을 쓴다(결정 8)."""
    if not isinstance(value, dict):
        value = {}
    defaults = DEFAULT_CONFIG["compile"]
    result = dict(defaults)
    result["mode"] = value.get("mode") if value.get("mode") in COMPILE_MODES else defaults["mode"]
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
    # sourceScan은 `wiki_lifecycle.run`이 `enabled`·`maxCardsPerScan`을 읽는다. `enabled`는
    # bool만 받고 그 밖의 값은 기본값(True = 수명 주기 유지)이다.
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
        "backends": dict(default_extractor["backends"]),
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
    result["budget"] = {
        "extractorPerRun": coerce_capped_int(
            budget.get("extractorPerRun", default_budget["extractorPerRun"]),
            default_budget["extractorPerRun"], MAX_COMPILE_PER_RUN,
        ),
        "cardsPerSource": coerce_capped_int(
            budget.get("cardsPerSource", default_budget["cardsPerSource"]),
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
            "crossEngine": default_judge["crossEngine"],
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
        "onFail": default_verify["onFail"],
        "onInconclusive": default_verify["onInconclusive"],
        "crossEngine": default_verify["crossEngine"],
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


def diagnostics_config(value):
    """`diagnostics` 블록 → `{"enabled": bool}`. **JSON boolean `true`만 켠다.**

    키 없음·`false`·문자열 `"true"`·`1`·`1.0`·블록이 dict가 아님 등 나머지는 전부 off다.
    상세 진단은 예외 원문(민감 정보 가능)을 기록하므로 모호한 값을 켜는 쪽으로 읽지 않는다.
    `1 == True`이므로 `== True`가 아니라 `is True`로 판정한다. 항상 새 dict를 반환해
    입력을 변경하거나 공유하지 않는다.
    """
    enabled = isinstance(value, dict) and value.get("enabled") is True
    return {"enabled": enabled}


def invalid_session_start_sync(input_config):
    """A present non-boolean opt-out is invalid and must not become default-on."""
    return (isinstance(input_config, dict) and "sessionStartSync" in input_config
            and not isinstance(input_config["sessionStartSync"], bool))


def normalize_config(input_config, folder=None):
    """`folder`는 프로젝트의 활성 폴더 이름이다. 주면 minimal 펼침의 wiki 경로와 두 폴더
    이름 중 하나로 시작하는 `wikiPath`·`collectionPaths` 값이 그 폴더를 따른다. 생략하면
    minimal 펼침과 `wikiPath` 기본값은 `.memvane`이고 적힌 값은 그대로다."""
    minimal = is_minimal_settings(input_config)
    if minimal:
        _, learning_reason = _learning_blocks(input_config)
        input_config = expand_minimal_settings(input_config, folder)
    config = _normalize_expanded_config(input_config)
    if minimal:
        config["settingsModel"] = MINIMAL_SETTINGS_MODEL
        if learning_reason is not None:
            config["settingsNotes"] = [learning_reason]
    return _rebase_folder(config, folder)


def _rebase_folder(config, folder):
    # Without a known project folder explicit values are kept as written.
    if folder is None or not project_dir.is_folder_name(folder):
        return config

    def moved(value):
        if not isinstance(value, str):
            return value
        parts = value.split("/")
        if parts and parts[0] in project_dir.NAMES and parts[0] != folder:
            return "/".join([folder, *parts[1:]])
        return value
    config["wikiPath"] = moved(config.get("wikiPath"))
    paths = config.get("collectionPaths")
    if isinstance(paths, dict):
        config["collectionPaths"] = {key: moved(value) for key, value in paths.items()}
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
    config["diagnostics"] = diagnostics_config(input_config.get("diagnostics"))
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


def _candidate_configs(directory):
    # Read `.memvane/settings.json`.
    return [(path, "auto-context-dir", directory)
            for path in project_dir.settings_candidates(directory)]


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
            config = normalize_config(parsed if is_minimal_settings(parsed) else {},
                                      project_dir.active_name(project_root))
            if config.get("indexing") is False:
                config["collections"] = []
            return {
                "config": config,
                "configPath": str(config_file),
                "configFormat": config_format,
                "projectRoot": str(project_root),
                "projectOwner": str(project_root),
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
    """Load the effective config from the nearest `.memvane/settings.json`.
    indexing:false means collections=[] (검색/인덱싱 skip). 못 찾으면 빈 설정(collections=[])."""
    return find_project_config(cwd)["config"]


def load_project_config_raw(cwd):
    """Return the raw JSON object for the discovered project config, or {}.

    지원하는 settings는 내부 설정 모양으로 펼치고 `settingsModel`을 붙인다.
    지원하지 않는 설정은 빈 dict다."""
    found = find_project_config(cwd)
    config_path = found.get("configPath")
    if not config_path:
        return {}
    parsed, reason = _read_json_object(config_path)
    if reason:
        return {}
    if found.get("configFormat") == "auto-context-dir" and is_minimal_settings(parsed):
        folder = project_dir.active_name(found.get("projectRoot"))
        expanded = expand_minimal_settings(parsed, folder)
        return {**expanded, "settingsModel": MINIMAL_SETTINGS_MODEL}
    return {}


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
