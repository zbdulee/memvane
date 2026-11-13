"""Frontmatter and managed-card ownership checks."""
from __future__ import annotations

from memvane.wiki.card import yaml_scalars, wiki_markers
from memvane.wiki.card.types import FRONTMATTER_RE, AUTO_BLOCK_RE

def unquote_yaml(value: str) -> str:
    # 인용/역인용 규칙은 core/yaml_scalars.py가 SSOT다 — recall의 읽기 파서가 같은
    # 함수를 쓰게 해서 이스케이프 규칙이 갈리지 않게 한다(title 오염의 원인).
    return yaml_scalars.load(value)

def parse_yaml_scalar(value: str):
    text = unquote_yaml(value)
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    return text

def parse_yaml_inline_list(value: str) -> list:
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return []
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [parse_yaml_scalar(part.strip()) for part in inner.split(",") if part.strip()]

def parse_frontmatter(text: str) -> tuple[dict, bool]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, False
    meta = {}
    current_key = None
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip():
            continue
        if raw_line.startswith("  - "):
            if current_key is None:
                return {}, False
            meta.setdefault(current_key, []).append(parse_yaml_scalar(raw_line[4:]))
            continue
        if raw_line.startswith(" ") or ":" not in raw_line:
            return {}, False
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        if not key:
            return {}, False
        if key in meta:
            return {}, False
        raw_value = raw_value.strip()
        if raw_value == "":
            meta[key] = []
            current_key = key
        elif raw_value.startswith("[") and raw_value.endswith("]"):
            meta[key] = parse_yaml_inline_list(raw_value)
            current_key = None
        else:
            meta[key] = parse_yaml_scalar(raw_value)
            current_key = None
    return meta, True

def auto_writable_text(text: str) -> tuple[bool, list[str]]:
    """Plugin ownership of one card's bytes (createdBy + managed block, not superseded)."""
    meta, ok = parse_frontmatter(text)
    if not ok:
        return False, ["frontmatter_unparseable"]
    findings = []
    # verified remains auto-writable so source changes reset it to generated.
    # superseded alone is historical and must never be revived by a refresh.
    if str(meta.get("status") or "").strip().lower() == "superseded":
        findings.append("protected_status")
    if meta.get("createdBy") not in wiki_markers.CREATED_BY_NAMES:
        findings.append("non_qmd_created_by")
    if not AUTO_BLOCK_RE.search(text):
        findings.append("managed_section_missing")
    return not findings, findings
