"""Atomic source revision metadata updates for wiki cards."""
from __future__ import annotations

from pathlib import Path

from memvane.kernel.atomic_rename import write_text_atomic
from memvane.wiki.card import api as yaml_scalars
from memvane.wiki.card.api import frontmatter_sections as _frontmatter_sections
from memvane.wiki.card.types import FRONTMATTER_RE


def insert_source_revisions(path: Path, revisions: list) -> bool:
    """Add the compiler-owned ``sourceRevisions`` block to a card that has none.

    Kept out of ``patch_frontmatter_fields`` because that function is a scalar
    patcher by construction: ``frontmatter_patch_scalar`` runs ``str(value)``
    through ``yaml_scalars.dump``, so a list lands as one quoted scalar line, and
    its ``None`` removal path drops only the ``key:`` line and would orphan a
    block value's indented children.  Teaching it a second shape puts its only
    scalar caller (``stamp_verification``) behind a branch it does not need.  Block-shaped frontmatter surgery already has a
    primitive here — ``_frontmatter_sections`` — and ``rewrite_generated_card``
    uses it exactly this way.  Serialization stays in ``yaml_scalars``.

    **Fail-closed on any card that already declares the key, parseable or not.**
    ``recall.frontmatter_source_revisions`` reports a malformed block as "no
    proof", so keying off the parsed value would append a SECOND top-level
    header — which that same parser rejects as a duplicate declaration, leaving
    the card corrupted and still untrusted.  Only the structural key scan of
    ``_frontmatter_sections`` separates "absent" from "present but unusable".

    The block goes directly after ``sources:`` so a backfilled card is
    byte-shaped like one ``markdown_page`` writes; a card with no ``sources``
    section has no provenance to extend and is refused.
    """
    emitted = yaml_scalars.dump_source_revisions(revisions)
    if not emitted:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    sections = _frontmatter_sections(match.group(1))
    if sections is None:
        return False
    keys = {key for key, _ in sections}
    if "sourceRevisions" in keys or "sources" not in keys:
        return False
    merged: list[str] = []
    for key, lines in sections:
        merged.extend(lines)
        if key == "sources":
            merged.append("sourceRevisions:")
            merged.extend(f"  - {revision}" for revision in emitted)
    patched = text[: match.start(1)] + "\n".join(merged) + text[match.end(1):]
    # 원자적 쓰기 — 이 쓰기는 카드에 recall 신뢰를 부여하므로 반환값을 삼키면
    # 호출부가 "신뢰 부여됨"으로 보고하는데 카드는 그대로인 상태가 된다.
    return write_text_atomic(path, patched)
