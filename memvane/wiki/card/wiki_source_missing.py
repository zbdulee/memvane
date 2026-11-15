#!/usr/bin/env python3
"""카드 소스 존재 판정 helper — wiki root 격리, allow-root, 소스 분류.

존재 판정은 `wiki_card.resolve_existing_source`(주입·검수·수명 주기 공용 SSOT) 하나만
쓴다. 판정이 갈리면 "주입은 링크를 지웠는데 검수는 정상으로 본다"가 된다. 소실된
카드의 처리(보관 후 삭제, 재생성 enqueue)는 SessionStart `wiki_lifecycle`이 한다.
이 모듈은 카드를 수정하거나 원장을 쓰지 않는다.
"""

from __future__ import annotations
from pathlib import Path
from memvane.settings import types as project_types
from memvane.wiki.card import wiki_card
from memvane.settings import api as qmd_resolve_paths
from memvane.wiki.card import yaml_scalars

# 카드가 있는데 소스 항목을 하나도 파일로 읽을 수 없는 상태를 "소실"로 볼 수 없는 사유.
# 파싱 실패·루트 밖·길이 초과는 "없다"가 아니라 "판정 불가"이므로 소실로 세지 않는다 —
# 미지원 표기(block mapping·여러 줄 flow)를 소실로 분류하면 "카드를 고치면 되는 표기
# 문제"와 "원문이 사라졌다"가 섞인다.


def wiki_root_of(root: Path, config: dict) -> Path | None:
    """`wikiPath` → 실경로. **root 격리 검사까지 포함한 SSOT.**

    수명 주기·검수·백필이 같은 판정을 쓴다. 디렉터리 심볼릭 링크와 `wikiPath:"../x"`는
    `resolve_paths.contained_path`(resolve **후** 포함 검사)가 거부한다. **allowRoots는
    넘기지 않는다** — wiki는 관리 대상 디렉터리라 프로젝트 밖으로 나갈 수 없다
    (`safe_managed_dir`와 같은 강도).
    """
    rel = config.get("wikiPath", project_types.WIKI_REL)
    if not isinstance(rel, str) or not rel:
        rel = project_types.WIKI_REL
    return qmd_resolve_paths.contained_path(root, rel, [])


SOURCES_OK = "ok"
SOURCES_ABSENT = "absent"
from memvane.wiki.card.types import SOURCES_INVALID


def declared_source_records(block: str) -> tuple[list[dict], str]:
    """카드 frontmatter 의 `sources` → (레코드 목록, 판정).

    판정은 셋이다 — `ok`(전 항목을 읽었다, 빈 목록 포함), `absent`(`sources` 키 없음),
    `invalid`(키는 있는데 무엇을 놓쳤는지 알 수 없다). "빈 목록"과 "읽을 수 없다"를 뭉치면
    미지원 표기(block mapping·여러 줄 flow)를 쓴 카드가 "소스 없음"으로 보여 그 카드를
    근거로 한 판정(백필·퇴역)이 통째로 우회된다. 같은 이유로 파서가 본 항목 수와 실제
    줄 수가 어긋나도 `invalid` 다. 비파일 항목(session/url)은 레코드로 그대로 낸다.
    """
    sections = yaml_scalars.frontmatter_sections(block)
    if sections is None:
        return [], SOURCES_INVALID
    lines = None
    for key, section_lines in sections:
        if key == "sources":
            lines = section_lines
            break
    if lines is None:
        return [], SOURCES_ABSENT
    inline = wiki_card.sources_inline_entry(lines[0].split(":", 1)[1])
    children = [line for line in lines[1:] if line.strip()]
    if any(not line.strip().startswith("- ") for line in children):
        return [], SOURCES_INVALID
    entries = wiki_card.frontmatter_source_entries(block)
    if len(entries) != len(children) + (1 if inline else 0):
        return [], SOURCES_INVALID
    records = []
    for raw in entries:
        record, problem = yaml_scalars.load_flow_mapping(raw)
        if problem or not isinstance(record, dict):
            return [], SOURCES_INVALID
        if record.get("kind") == wiki_card.SOURCE_KIND_FILE:
            path = record.get("path")
            if not isinstance(path, str) or not path:
                return [], SOURCES_INVALID
        records.append(record)
    return records, SOURCES_OK


def frontmatter_block(text: str) -> str | None:
    match = yaml_scalars.FRONTMATTER_RE.match(text)
    return match.group(1) if match else None


def missing_key(paths) -> list[str]:
    """소실 경로 집합의 정규형 — 상태 비교(같은 소실인가)의 기준."""
    return sorted({p for p in paths if isinstance(p, str) and p})










def allow_roots_of(config: dict) -> list[Path]:
    return qmd_resolve_paths.allowed_roots(config)
