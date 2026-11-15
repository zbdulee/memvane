"""Top-level frontmatter field rendering for card text."""
from __future__ import annotations

import re

from memvane.settings import types as settings_types
from memvane.wiki.card import yaml_scalars
from memvane.wiki.card.types import FRONTMATTER_RE, SOURCE_SPANS_KEY
from memvane.wiki.card.yaml_scalars import dump as yaml_scalar


def frontmatter_patch_scalar(key: str, value) -> str:
    text = str(value)
    if key == "status" and text in settings_types.WIKI_STATUSES and re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return text
    return yaml_scalar(value)


def _render_frontmatter_fields(text: str, updates: dict, blocks: dict | None = None) -> str | None:
    blocks = dict(blocks or {})
    if "status" in updates:
        blocks.setdefault(SOURCE_SPANS_KEY, None)
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    lines = match.group(1).splitlines()
    seen = set()
    new_lines = []
    in_block = False
    for line in lines:
        if in_block:
            if not line.strip() or line[0].isspace():
                continue
            in_block = False
        # 블록 헤더 판정은 읽는 쪽(`yaml_scalars.closed_block_entries`)과 같은 규칙이다.
        if line and not line[0].isspace() and yaml_scalars.load(line.split(":", 1)[0].strip()) in blocks:
            in_block = True
            continue
        key = None
        if line and not line.startswith(" ") and ":" in line:
            key = line.split(":", 1)[0].strip()
        if key in updates:
            seen.add(key)
            if updates[key] is None:
                continue
            new_lines.append(f"{key}: {frontmatter_patch_scalar(key, updates[key])}")
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen and value is not None:
            new_lines.append(f"{key}: {frontmatter_patch_scalar(key, value)}")
    for key, entries in blocks.items():
        if entries:
            new_lines.append(f"{key}:")
            new_lines.extend(f"  - {entry}" for entry in entries)
    new_frontmatter = "\n".join(new_lines)
    return text[: match.start(1)] + new_frontmatter + text[match.end(1) :]
