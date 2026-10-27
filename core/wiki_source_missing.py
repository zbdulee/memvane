#!/usr/bin/env python3
"""카드 소스 존재 판정 helper — wiki root 격리, allow-root, 소스 분류.

존재 판정은 `recall.resolve_existing_source`(주입·검수·수명 주기 공용 SSOT) 하나만
쓴다. 판정이 갈리면 "주입은 링크를 지웠는데 검수는 정상으로 본다"가 된다. 소실된
카드의 처리(보관 후 삭제, 재생성 enqueue)는 SessionStart `wiki_lifecycle`이 한다.
이 모듈은 카드를 수정하거나 원장을 쓰지 않는다.
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import recall as qmd_recall
import resolve_paths as qmd_resolve_paths
import wiki_compile as wc

# 카드가 있는데 소스 항목을 하나도 파일로 읽을 수 없는 상태를 "소실"로 볼 수 없는 사유.
# 파싱 실패·루트 밖·길이 초과는 "없다"가 아니라 "판정 불가"이므로 소실로 세지 않는다 —
# 미지원 표기(block mapping·여러 줄 flow)를 소실로 분류하면 "카드를 고치면 되는 표기
# 문제"와 "원문이 사라졌다"가 섞인다.
IGNORED_ENTRY_REASONS = qmd_recall.NON_FILE_SOURCE_REASONS


def wiki_root_of(root: Path, config: dict) -> Path | None:
    """`wikiPath` → 실경로. **root 격리 검사까지 포함한 SSOT.**

    수명 주기·검수·백필이 같은 판정을 쓴다. 디렉터리 심볼릭 링크와 `wikiPath:"../x"`는
    `resolve_paths.contained_path`(resolve **후** 포함 검사)가 거부한다. **allowRoots는
    넘기지 않는다** — wiki는 관리 대상 디렉터리라 프로젝트 밖으로 나갈 수 없다
    (`safe_managed_dir`와 같은 강도).
    """
    rel = config.get("wikiPath", ".auto-context/wiki")
    if not isinstance(rel, str) or not rel:
        rel = ".auto-context/wiki"
    return qmd_resolve_paths.contained_path(root, rel, [])


def frontmatter_block(text: str) -> str | None:
    match = wc.FRONTMATTER_RE.match(text)
    return match.group(1) if match else None


def missing_key(paths) -> list[str]:
    """소실 경로 집합의 정규형 — 상태 비교(같은 소실인가)의 기준."""
    return sorted({p for p in paths if isinstance(p, str) and p})


def classify_entries(entries, project_root: Path, allow_roots: list[Path]) -> dict:
    """sources 표기 목록 → {present, missing, unknown, entries}.

    항목별 판정은 `recall.classify_source_entry` 하나만 쓴다(주입과 같은 판정).
    """
    present: list[str] = []
    missing: list[str] = []
    unknown: dict[str, int] = {}
    for entry in entries:
        resolved, reason, raw_path = qmd_recall.classify_source_entry(
            entry, project_root, allow_roots)
        if resolved is not None:
            present.append(raw_path)
        elif reason == "missing":
            missing.append(raw_path)
        elif reason in IGNORED_ENTRY_REASONS:
            continue
        else:
            unknown[reason] = unknown.get(reason, 0) + 1
    return {"present": present, "missing": missing, "unknown": unknown,
            "entries": len(entries)}


def classify_card(text: str, project_root: Path, allow_roots: list[Path]) -> dict:
    """카드 본문 전체 → 소스 상태. frontmatter가 없으면 판정하지 않는다."""
    block = frontmatter_block(text)
    if block is None:
        return {"present": [], "missing": [], "unknown": {"frontmatter_missing": 1},
                "entries": 0}
    return classify_entries(
        qmd_recall.frontmatter_source_entries(block), project_root, allow_roots)


def classify_records(sources, project_root: Path, allow_roots: list[Path]) -> dict:
    """`sources` **레코드(dict) 목록** → 같은 판정.

    큐 잡(`wiki_verify_worker`)·candidate는 frontmatter 문자열이 아니라 dict를 들고 있다.
    표기 파싱만 건너뛰고 존재 판정은 동일 함수(`recall.resolve_existing_source`)를 쓴다.
    """
    present: list[str] = []
    missing: list[str] = []
    unknown: dict[str, int] = {}
    entries = 0
    if not isinstance(sources, list):
        return {"present": present, "missing": missing, "unknown": unknown, "entries": 0}
    for src in sources:
        if not isinstance(src, dict):
            continue
        entries += 1
        if src.get("kind") != qmd_recall.SOURCE_KIND_FILE:
            continue
        raw_path = src.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        resolved, reason = qmd_recall.resolve_existing_source(
            raw_path, project_root, allow_roots)
        if resolved is not None:
            present.append(raw_path)
        elif reason == "missing":
            missing.append(raw_path)
        else:
            unknown[reason] = unknown.get(reason, 0) + 1
    return {"present": present, "missing": missing, "unknown": unknown, "entries": entries}


def all_sources_missing(info: dict) -> bool:
    """"소스 전부 소실" 판정 — 규칙은 `recall.sources_all_missing`(SSOT)에 있다.

    소실 항목이 하나 이상이고, 살아 있는 파일 소스가 하나도 없고, 판정 불가 항목도 없을
    때만 참이다. **일부만 소실은 대상이 아니다**(살아 있는 원문으로 대조가 가능하므로
    "stale 링크가 유일 진실"이 성립하지 않는다). 소스 항목이 0인 메타 카드
    (SCHEMA/index/log)도 대상이 아니다 — 소실이 0이라 자동으로 빠진다.
    이 경로는 항목을 **전부** 조사하므로 recall의 stat 예산 절단이 없다(그 차이는
    `recall.sources_all_missing` docstring 참고).
    """
    return qmd_recall.sources_all_missing(
        len(info.get("missing") or ()), len(info.get("present") or ()),
        sum((info.get("unknown") or {}).values()))


def allow_roots_of(config: dict) -> list[Path]:
    return qmd_resolve_paths.allowed_roots(config)
