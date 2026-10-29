#!/usr/bin/env python3
"""Single source of the wiki auto-compile config block.

--enable-compile, --init-wiki (recall stays separate), and recommend_config all
use these so onboarding paths agree on portable built-in engine names and
compile defaults. The worker resolves built-in adapter paths at runtime.

**생성기는 delta만 쓴다.** `compile_block()`이 돌려주는 것은 `full_compile_block()`에서
`config.DEFAULT_CONFIG["compile"]`과 **완전히 같은 값의 키를 제거한** 결과다. 생략된 키는
`normalize_config()`가 같은 값으로 채우므로 effective config는 움직이지 않고(그 불변식을
`test/config-emission-freeze.test.mjs`가 동결한다) 사용자가 관리해야 하는 표면만 줄어든다.
생성기가 기본값을 통째로 복사해 두면 나중에 기본값을 고쳐도 온보딩한 프로젝트만 옛 값에
고정돼, "기본값과 다른 리터럴"이 파일 안에 박제된다 — 이 파일이 그 클래스의 발생원이었다.

활성화는 `compile.mode` 한 값이 담당한다(`enabled`·`autoWrite`는 스키마에서 제거됐다).
`extractor.builtins`는 기본값이 `[]`라 delta에 남고, `dispatch`/`default`/`argv`는 더 이상
존재하지 않는다 — `dispatch` 게이트가 있던 동안 그 키를 빠뜨린 생성기 출력은 엔진이 하나도
해석되지 않아 큐에 든 잡이 `missing_extractor`로 **폐기**됐다.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import config as qmd_config

ENGINES = ("claude", "codex", "hermes")


def plugin_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT")
    if env:
        return Path(env).resolve()
    # this file lives at <root>/core/wiki_compile_defaults.py
    return Path(__file__).resolve().parents[1]


def parse_engines(value: str | None) -> tuple[str, ...]:
    if not value:
        return ENGINES
    picked = tuple(e for e in (s.strip() for s in value.split(",")) if e in ENGINES)
    return picked or ENGINES


def builtin_engines(engines=ENGINES) -> list[str]:
    picked = [e for e in engines if e in ENGINES]
    return picked or list(ENGINES)


def same_as_default(value, default) -> bool:
    """생성기 값이 기본값과 **완전히** 같은가.

    bool은 별도로 본다 — 파이썬에서 `True == 1`이라, 기본값이 숫자인 자리에 bool을 쓰는
    (또는 그 반대의) 생성기 값이 "같다"로 판정돼 조용히 생략되는 것을 막는다.
    """
    if isinstance(value, bool) != isinstance(default, bool):
        return False
    return value == default


def prune_defaults(value: dict, defaults) -> dict:
    """`defaults`와 같은 값의 키를 재귀적으로 제거한 사본을 돌려준다.

    - 기본값에 없는 키는 비교할 대상이 없으므로 남긴다.
    - 하위 dict는 재귀적으로 줄이고, 전부 기본값이면 그 키 자체를 뺀다.
    """
    if not isinstance(defaults, dict):
        return dict(value)
    pruned = {}
    for key, item in value.items():
        if key not in defaults:
            pruned[key] = item
            continue
        default = defaults[key]
        if isinstance(item, dict) and isinstance(default, dict):
            sub = prune_defaults(item, default)
            if sub:
                pruned[key] = sub
            continue
        if not same_as_default(item, default):
            pruned[key] = item
    return pruned


def compile_defaults() -> dict:
    block = qmd_config.DEFAULT_CONFIG.get("compile")
    return block if isinstance(block, dict) else {}


def default_valued_compile_keys(root, engines=ENGINES) -> list:
    """생성기 값이 기본값과 같아 `compile_block()`이 생략한 최상위 키들.

    `--enable-compile`은 예전에 전체 블록으로 기존 설정을 **덮어썼다**. delta로 바꾸면서
    그 키들을 그냥 안 쓰기만 하면, 기존 설정에 남아 있던 비기본값이 살아남아 effective가
    달라진다(예전: block이 이겨서 기본값으로 리셋). 그래서 병합 쪽은 이 목록을 지운다 —
    "키 없음 → 기본값"이 예전의 "기본값을 명시" 와 같은 결과다.
    """
    full = full_compile_block(root, engines)
    delta = compile_block(root, engines)
    return [key for key in full if key not in delta]


def compile_block(root, engines=ENGINES) -> dict:
    """온보딩이 실제로 쓰는 delta 블록 (기본값과 같은 키는 빠진다)."""
    return prune_defaults(full_compile_block(root, engines), compile_defaults())


def full_compile_block(root, engines=ENGINES) -> dict:
    """생성기가 의도하는 **완전한** compile 설정. 기본값과 겹치는 부분을 포함한다.

    직접 쓰지 말 것 — 파일로 나가는 것은 `compile_block()`의 delta다. 이 함수는 의도를
    한 곳에 적어 두고 delta를 계산하기 위한 입력이다.
    """
    return {
        "mode": "auto-wiki",
        "defaultStatus": "generated",
        "triggers": ["post_tool_source", "post_sync_source", "manual"],
        "maxSourceChars": 12000,
        "excludeStatusesFromRecall": ["discarded", "contested"],
        "lowPriorityStatuses": ["generated", "tentative"],
        "maxAutoPageLines": 120,
        # Derive policy from the runtime defaults so config emission cannot
        # pin an obsolete effort or cross-host policy after a default change.
        "reasoningEffort": copy.deepcopy(compile_defaults()["reasoningEffort"]),
        "extractor": {
            "backends": {},
            "builtins": builtin_engines(engines),
            "timeout": 120,
            "cooldownSeconds": 600,
        },
        "batch": {"idleSeconds": 90, "maxItems": 5},
        "verify": copy.deepcopy(compile_defaults()["verify"]),
    }
