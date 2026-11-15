"""Machine-review stamp: the verification status and its proof fields."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from memvane.settings import types as settings_types
from memvane.wiki.card import api as yaml_scalars
from memvane.wiki.card.api import render_frontmatter_fields as _render_frontmatter_fields
from memvane.wiki.card.types import SOURCE_SPANS_KEY
from memvane.wiki.workspace import api as workspace_files
from memvane.wiki.workspace.api import write_text_atomic


@workspace_files.guard_path
def stamp_verification(path: Path, status: str, engine: str, mode: str,
                       body_hash: str | None, source_spans: list | None = None) -> bool:
    """Write a machine-review outcome — status AND all three proof fields together.

    Single writer so a status can never land without its proof. The live corpus shows
    what the split costs: 27 `status: verified` cards carry no `verifiedAt` at all
    (all of them `verifiedBy: agent-full-source`, a string this codebase never
    writes — a hand/agent backfill that stamped the status and skipped the proof).

    `verifiedMode` records WHO checked relative to who wrote (see
    wiki_verify_worker.VERIFIED_MODE_*): a card verified by the same engine that
    produced it is a self-review, and self-preference bias makes that a weaker
    claim than a cross-engine pass. The field is absent on every card verified
    before this existed — absence therefore means "unknown, most likely self"
    (measured 655/688), never "cross-engine".

    `source_spans` (verifier-cited line spans, `source_spans.derive_spans`) are
    written in the **same atomic write** as the status, and only on `verified`.
    Every stamp without spans removes an existing list — contested, a pass with
    no locatable quote, and every other writer of this function alike — so a
    span list always belongs to the verdict stamped next to it. The list must be
    complete and bound to this card's own `sourceRevisions`; otherwise nothing is
    written (no partial proof).
    """
    expected_text = None
    if source_spans:
        try:
            expected_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
    rendered = render_verification(path, status, engine, mode, body_hash, source_spans,
                                   datetime.now(timezone.utc))
    if expected_text is not None:
        try:
            if path.read_text(encoding="utf-8") != expected_text:
                return False
        except (OSError, UnicodeError):
            return False
    return rendered is not None and write_text_atomic(path, rendered)


def render_verification(path: Path, status: str, engine: str, mode: str,
                        body_hash: str | None, source_spans: list | None, now: datetime) -> str | None:
    """Render one verification stamp without mutating the card."""
    try:
        expected_text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    except UnicodeError:
        if source_spans:
            return None
        raise
    emitted = None
    if source_spans:
        emitted = yaml_scalars.dump_source_spans(source_spans)
        if status != "verified" or not emitted:
            return None
        from memvane.wiki.card import api as spans_module
        if not spans_module.bound_to_revisions(
                yaml_scalars.load_source_spans(emitted) or [],
                spans_module.card_revisions(expected_text)):
            return None
    return _render_frontmatter_fields(expected_text, {
        "status": status,
        "verifiedBy": str(engine or "unknown"),
        # Pending-refresh events retain microseconds. Verification stamps need
        # the same precision so the index worker can distinguish a completed
        # card from a sibling written during an unfinished event.
        "verifiedAt": now.isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
        "verifiedMode": mode if mode in settings_types.VERIFIED_MODES else settings_types.VERIFIED_MODE_UNKNOWN,
        # 검증기가 실제로 읽은 본문의 지문. **호출자가 넘긴다** — 여기서 파일을 다시 읽어
        # 계산하면 검수 중에 본문이 바뀐 경우 "검증되지 않은 새 본문"에 verified 가 찍힌다.
        # 호출자 값이면 그 경우 디스크와 불일치해 recall 이 fail-closed 로 거른다.
        # None 이면 키를 쓰지 않는다(`patch_frontmatter_fields` 규약) = 결속 없는 legacy
        # 형태이고, 그것이 필요한 경로는 지금 없다(양 호출부 모두 값을 넘긴다).
        "verifiedBodyHash": body_hash,
    }, blocks={SOURCE_SPANS_KEY: emitted})


def write_rendered_verification(path: Path, rendered: str, expected_text: str) -> bool:
    """Write the previously rendered stamp only while its input remains current."""
    try:
        if path.read_text(encoding='utf8') != expected_text:
            return False
    except (OSError, UnicodeError):
        return False
    return write_text_atomic(path, rendered)
