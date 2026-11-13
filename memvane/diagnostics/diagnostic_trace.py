"""Detailed diagnostic consent (opt-in) for one install.

The supplied consent callback decides whether a new detailed diagnostic may be
recorded for the install the given route decision proves. Capture checks it
before collecting and again right before writing.

Contract:

- The only source of consent is the project's own ``settings.json``
  ``diagnostics.enabled``, and only the JSON boolean ``true`` turns it on
  (``config.diagnostics_config``). The settings are re-read on every call; no
  verdict is cached and the decision object is never mutated, so switching the
  setting off stops every later diagnostic.
- The consenting project is the one a *proved* route names: a ``candidate`` or
  ``preview`` decision, or a ``worktree`` decision whose ``mainRoute`` is itself
  a candidate/preview. A linked worktree reads its main checkout's settings,
  never its own. A bare reader fence hint (``state: reader``), a nested
  worktree, a stale/blocked/legacy route, or any malformed shape is off.
- Every failure is off: malformed decision, missing or non-canonical project
  root, settings that are absent, symlinked, unreadable, partially written or
  not a JSON object, settings discovered somewhere other than the proved root,
  or any exception at all. It never raises.
- It only stats and reads files. It never spawns a process, opens a network
  connection, or creates a file or directory.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import traceback

from memvane.diagnostics import event_log

__all__ = ["capture", "reassemble"]

# Bounded fragment format (Task 2). A capture is ``parts`` rows of event
# ``diagnostic.part`` (index, base64 ``data``) followed by one
# ``diagnostic.header`` row (kind, parts, bytes, sha256, truncated). The header
# is written last, so its presence means every part write succeeded. Each row
# goes through ``event_log.emit`` (same lock, rotation and MAX_LINE cap).
TEXT_LIMIT = 16 * 1024
PART_CHARS = 240
PART_RAW = PART_CHARS // 4 * 3  # 180 raw bytes -> exactly 240 base64 chars
MAX_PARTS = 96
STACK_LIMIT = 64
HEADER_EVENT = "diagnostic.header"
PART_EVENT = "diagnostic.part"
CHILD_TAIL_LIMIT = 4096  # bytes of a child's stderr tail (not its full stack)
CHILD_KIND = "child_stderr_tail"
_KINDS = frozenset({"exception", "origin_stack", CHILD_KIND})
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")

# Route states whose decision carries a verified install binding.














# ------------------------------------------------------------------ capture

def _shorten_frames(frames):
    """Keep useful source location without exposing an absolute home path."""
    for frame in frames:
        if os.path.isabs(frame.filename):
            parts = Path(frame.filename).parts
            frame.filename = '.../' + '/'.join(parts[-3:])


def _shorten_exception_frames(item, seen=None):
    seen = set() if seen is None else seen
    if item is None or id(item) in seen:
        return
    seen.add(id(item))
    _shorten_frames(item.stack)
    _shorten_exception_frames(item.__cause__, seen)
    _shorten_exception_frames(item.__context__, seen)
    for child in getattr(item, 'exceptions', None) or ():
        _shorten_exception_frames(child, seen)


def _format(exc, warning: bool) -> tuple[str, str] | None:
    """(kind, text): an exception chain, or the caller's origin stack. No locals."""
    if exc is not None and isinstance(exc, BaseException):
        item = traceback.TracebackException(type(exc), exc, exc.__traceback__,
                                            limit=STACK_LIMIT, capture_locals=False)
        _shorten_exception_frames(item)
        return "exception", "".join(item.format(chain=True))
    if warning:
        # Drop this module's own frames (capture/_format) from the origin stack.
        frames = [frame for frame in traceback.extract_stack(limit=STACK_LIMIT + 4)
                  if os.path.abspath(frame.filename) != os.path.abspath(__file__)]
        _shorten_frames(frames)
        return "origin_stack", ("Origin stack (most recent call last):\n"
                                + "".join(traceback.format_list(frames[-STACK_LIMIT:])))
    return None


def _bounded(text: str) -> tuple[bytes, bool]:
    """UTF-8 bytes of at most TEXT_LIMIT, cut on a character boundary."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= TEXT_LIMIT:
        return raw, False
    return raw[:TEXT_LIMIT].decode("utf-8", "ignore").encode("utf-8"), True


def _child_tail(text) -> tuple[bytes, bool] | None:
    """The last CHILD_TAIL_LIMIT UTF-8 bytes of a child's stderr detail.

    Only a bounded tail; never presented as the child's complete stack.
    """
    if not isinstance(text, str) or not text:
        return None
    raw = text.encode("utf-8", "replace")
    if len(raw) <= CHILD_TAIL_LIMIT:
        return raw, False
    return raw[-CHILD_TAIL_LIMIT:].decode("utf-8", "ignore").encode("utf-8"), True


def capture(decision, *, event, reason, exc=None, warning=False,
            child_stderr=None, consent=None) -> str | None:
    """Record one detailed diagnostic; return its 32-hex ID, or None.

    ``child_stderr`` (explicit opt-in) records kind ``child_stderr_tail``: at
    most CHILD_TAIL_LIMIT bytes of a child's failure detail, not its stack.
    None when consent is off (checked before formatting and again right before
    the first write), nothing is capturable, or any write fails. Never raises
    and never alters the caller's outcome.
    """
    try:
        if consent is None or consent(decision) is not True:
            return None
        if child_stderr is not None:
            tail = _child_tail(child_stderr)
            if tail is None:
                return None
            kind = CHILD_KIND
            raw, truncated = tail
        else:
            formatted = _format(exc, bool(warning))
            if formatted is None:
                return None
            kind, text = formatted
            raw, truncated = _bounded(text)
        chunks = [raw[i:i + PART_RAW] for i in range(0, len(raw), PART_RAW)] or [b""]
        if len(chunks) > MAX_PARTS:
            return None
        diagnostic_id = os.urandom(16).hex()
        context = {"decision": decision, "diagnostic_id": diagnostic_id}
        if consent is None or consent(decision) is not True:
            return None
        for index, chunk in enumerate(chunks):
            data = base64.b64encode(chunk).decode("ascii")
            if not event_log.emit(PART_EVENT, outcome="part", reason=reason,
                                  index=index, data=data, **context):
                return None
        if not event_log.emit(HEADER_EVENT, outcome=kind, reason=reason,
                              source_event=event_log.code(event), kind=kind,
                              diagnostic_version=1,
                              parts=len(chunks), bytes=len(raw),
                              sha256=hashlib.sha256(raw).hexdigest(),
                              truncated=truncated, **context):
            return None
        return diagnostic_id
    except Exception:  # noqa: BLE001 - diagnostics never fail their caller
        return None


# -------------------------------------------------------------- reassembly

def reassemble(rows, diagnostic_id) -> dict:
    """Rebuild one diagnostic from event rows; ``incomplete`` on any defect.

    Missing, duplicate or corrupt parts or header, count, size or SHA-256
    mismatch, and invalid UTF-8 all yield ``{"status": "incomplete"}``.
    """
    incomplete = {"status": "incomplete", "diagnostic_id": diagnostic_id
                  if isinstance(diagnostic_id, str) and _ID.fullmatch(diagnostic_id)
                  else None}
    try:
        if incomplete["diagnostic_id"] is None or not isinstance(rows, (list, tuple)):
            return incomplete
        headers, parts = [], {}
        for row in rows:
            if not isinstance(row, dict) or row.get("diagnostic_id") != diagnostic_id:
                continue
            if row.get("event") == HEADER_EVENT:
                headers.append(row)
            elif row.get("event") == PART_EVENT:
                index = row.get("index")
                if type(index) is not int or index in parts:
                    return incomplete
                parts[index] = row
        if len(headers) != 1:
            return incomplete
        header = headers[0]
        count, size, digest = header.get("parts"), header.get("bytes"), header.get("sha256")
        kind, truncated = header.get("kind"), header.get("truncated")
        if (type(header.get("diagnostic_version")) is not int
                or header["diagnostic_version"] != 1
                or type(count) is not int or not 1 <= count <= MAX_PARTS
                or type(size) is not int or not 0 <= size <= TEXT_LIMIT
                or not isinstance(digest, str) or not _SHA.fullmatch(digest)
                or kind not in _KINDS or type(truncated) is not bool
                or (kind == CHILD_KIND and size > CHILD_TAIL_LIMIT)
                or sorted(parts) != list(range(count))):
            return incomplete
        chunks = []
        for index in range(count):
            data = parts[index].get("data")
            if not isinstance(data, str) or len(data) > PART_CHARS:
                return incomplete
            chunks.append(base64.b64decode(data.encode("ascii"), validate=True))
        raw = b"".join(chunks)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            return incomplete
        return {"status": "complete", "text": raw.decode("utf-8"),
                "truncated": truncated, "kind": kind}
    except (binascii.Error, UnicodeError, ValueError):
        return incomplete
    except Exception:  # noqa: BLE001 - reconstruction never raises
        return incomplete
