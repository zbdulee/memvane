#!/usr/bin/env python3
"""Verifier-cited source line spans for production wiki cards.

A machine-verify ``pass`` already carries ``claims`` with an exact ``quote`` and
a ``sourcePath``.  This module turns those quotes into line locations **in code**
-- no extra model call, no prompt change -- and only when the location is
unambiguous:

  - the claim says ``supported`` is exactly ``True``;
  - ``sourcePath`` is one of the worker-owned revision paths (decoys and other
    spellings of a path are ignored);
  - the quote occurs exactly once in the full source text (overlapping repeats
    count), and lies wholly inside the bounded prefix the verifier was shown.

Lines are split on ``\\n`` only.  UTF-8 never encodes ``\\n`` inside a
multibyte sequence, so character and byte line numbers agree.  A span records
``sha256`` of the whole selected line slice (``\\n`` terminators included) taken
from the actual source bytes, bound to the source revision ``sha256``.  It never
records quote text.

What a span means is deliberately narrow: it locates an excerpt the verifier
cited.  It is not per-claim proof, and a card whose quotes could not be located
keeps the existing file-level verified behaviour with no span key at all.
"""

from __future__ import annotations

import hashlib

import yaml_scalars

MAX_CLAIMS = 64
MAX_QUOTE_CHARS = 4000
# Larger sources keep file-level provenance only; the verifier still saw them.
MAX_SPAN_SOURCE_BYTES = 4 * 1024 * 1024
ABSENT = "absent"
PRESENT = "present"
INVALID = "invalid"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def line_slice(data: bytes, start: int, end: int) -> bytes | None:
    """Bytes of 1-based inclusive lines ``start..end``; ``None`` when out of range.

    The empty "line" after a trailing ``\\n`` (or in an empty file) is not a
    line, so a span can never point past the last real line.
    """
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        return None
    offset = 0
    for _ in range(start - 1):
        newline = data.find(b"\n", offset)
        if newline < 0:
            return None
        offset = newline + 1
    begin = offset
    if begin >= len(data):
        return None
    for _ in range(end - start):
        newline = data.find(b"\n", offset)
        if newline < 0 or newline + 1 >= len(data):
            return None
        offset = newline + 1
    newline = data.find(b"\n", offset)
    return data[begin:len(data) if newline < 0 else newline + 1]


def evidence_item(path: str, sha256: str, data: bytes, shown_chars: int) -> dict | None:
    """Bind one stable snapshot to the prefix length the verifier received."""
    if (not isinstance(data, bytes) or len(data) > MAX_SPAN_SOURCE_BYTES
            or type(shown_chars) is not int or shown_chars < 0 or _sha256(data) != sha256):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return {"path": path, "sha256": sha256, "data": data, "text": text,
            "shownChars": min(shown_chars, len(text))}


def _locate(quote: str, item: dict) -> dict | None:
    text = item["text"]
    first = text.find(quote)
    if first < 0 or text.find(quote, first + 1) >= 0:
        return None
    if first + len(quote) > item["shownChars"]:
        return None
    start = text.count("\n", 0, first) + 1
    end = text.count("\n", 0, first + len(quote) - 1) + 1
    if end - start + 1 > yaml_scalars.MAX_SPAN_LINES:
        return None
    selected = line_slice(item["data"], start, end)
    if selected is None:
        return None
    return yaml_scalars.normalize_source_span({
        "path": item["path"], "sourceSha256": item["sha256"],
        "startLine": start, "endLine": end, "spanSha256": _sha256(selected),
    })


def derive_spans(claims, evidence: dict) -> list[dict]:
    """Locate verifier quotes; unlocatable claims are skipped, never guessed."""
    if not isinstance(claims, list) or not isinstance(evidence, dict):
        return []
    found: dict[tuple, dict] = {}
    for claim in claims[:MAX_CLAIMS]:
        if not isinstance(claim, dict) or claim.get("supported") is not True:
            continue
        quote, path = claim.get("quote"), claim.get("sourcePath")
        if (not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS
                or not isinstance(path, str)):
            continue
        item = evidence.get(path)
        if not isinstance(item, dict) or item.get("path") != path:
            continue
        span = _locate(quote, item)
        if span is not None:
            found.setdefault((span["path"], span["startLine"], span["endLine"]), span)
        if len(found) >= yaml_scalars.MAX_SOURCE_SPANS:
            break
    return [found[key] for key in sorted(found)]


def _frontmatter_block(text: str) -> str | None:
    match = yaml_scalars.FRONTMATTER_RE.match(text) if isinstance(text, str) else None
    return match.group(1) if match else None


def read_card_spans(block: str) -> tuple[str, list[dict]]:
    """``(absent|present|invalid, spans)`` for one frontmatter body.

    ``present`` always carries a complete, schema-valid list; any defect in a
    present list is ``invalid`` with no partial result.
    """
    state, entries = yaml_scalars.closed_block_entries(
        block, yaml_scalars.SOURCE_SPANS_KEY, yaml_scalars.MAX_SOURCE_SPANS)
    if state != PRESENT:
        return state, []
    spans = yaml_scalars.load_source_spans(entries)
    if spans is None:
        return INVALID, []
    return PRESENT, sorted(spans, key=lambda s: (s["path"], s["startLine"], s["endLine"]))


def card_revisions(text: str) -> list[dict]:
    """The card's own ``sourceRevisions`` via the reader recall trusts."""
    block = _frontmatter_block(text)
    if block is None:
        return []
    import wiki_card
    return wiki_card.frontmatter_source_revisions(block)


def bound_to_revisions(spans, revisions) -> bool:
    """Every span names an exact ``(path, sha256)`` of the given revisions."""
    pairs = {(item.get("path"), item.get("sha256")) for item in revisions
             if isinstance(item, dict)}
    return all((span["path"], span["sourceSha256"]) in pairs for span in spans)


def bind_to_card(spans, text: str) -> list[dict]:
    """Keep only spans bound to the card's current ``sourceRevisions``."""
    revisions = card_revisions(text)
    return [span for span in spans if bound_to_revisions([span], revisions)]


def verify_span(span: dict, data: bytes) -> bool:
    """Current full source bytes still hash to the revision and the line slice."""
    if not isinstance(data, bytes) or _sha256(data) != span["sourceSha256"]:
        return False
    selected = line_slice(data, span["startLine"], span["endLine"])
    return selected is not None and _sha256(selected) == span["spanSha256"]


def locator(span: dict) -> str:
    return f'{span["path"]}#L{span["startLine"]}-L{span["endLine"]}'
