"""Detailed diagnostic consent (opt-in) for one install.

``enabled(decision)`` answers one question: may a *new* detailed diagnostic be
recorded for the install the given route decision proves? It is the gate later
capture code checks before collecting and again right before writing.

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

import config
import event_log
import project_dir

__all__ = ["enabled", "capture", "reassemble"]

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
_KINDS = frozenset({"exception", "origin_stack"})
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")

# Route states whose decision carries a verified install binding.
_PROVED_STATES = frozenset({"candidate", "preview"})
_WORKTREE_STATE = "worktree"
_WORKTREE_REASON = "linked_worktree_read_only"


def _canonical_dir(value) -> str | None:
    """An absolute, canonical (symlink-free) path of an existing directory."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if not os.path.isabs(value):
        return None
    try:
        if os.path.realpath(value) != value or not os.path.isdir(value):
            return None
    except (OSError, ValueError):
        return None
    return value


def _proved_root(route) -> str | None:
    """The project root of a candidate/preview decision, or None."""
    if not isinstance(route, dict) or route.get("state") not in _PROVED_STATES:
        return None
    install_id = route.get("installId")
    journal = route.get("journalPath")
    if not isinstance(install_id, str) or not install_id:
        return None
    if not isinstance(journal, str) or not os.path.isabs(journal):
        return None
    return _canonical_dir(route.get("projectRoot"))


def _consenting_root(decision) -> str | None:
    """The project whose settings decide consent for ``decision``."""
    if not isinstance(decision, dict):
        return None
    state = decision.get("state")
    if state in _PROVED_STATES:
        return _proved_root(decision)
    if state == _WORKTREE_STATE:
        if decision.get("reason") != _WORKTREE_REASON:
            return None
        if (_canonical_dir(decision.get("worktreeRoot")) is None
                or _canonical_dir(decision.get("worktreeProjectRoot")) is None):
            return None
        # Only a proved main route; a nested worktree or reader hint is off.
        return _proved_root(decision.get("mainRoute"))
    return None


def _settings_file(root: str) -> Path | None:
    """The regular, non-symlink settings file of exactly ``root``."""
    settings = project_dir.settings_path(root)
    if settings is None or settings.is_symlink() or not settings.is_file():
        return None
    return settings


def _project_opted_in(root: str) -> bool:
    """Re-read ``root``'s own settings now; True only for boolean true."""
    settings = _settings_file(root)
    if settings is None:
        return False
    # The settings must be discovered at the proved root itself: never a
    # parent's settings and never the empty fallback of an unreadable file.
    found = config.find_project_config(root)
    if (not isinstance(found, dict)
            or found.get("configFormat") != "auto-context-dir"
            or found.get("projectRoot") != root
            or found.get("configPath") != str(settings)):
        return False
    first = found.get("config")
    if not isinstance(first, dict) or config.diagnostics_config(
            first.get("diagnostics"))["enabled"] is not True:
        return False
    # Second read through the public loader: a concurrent writer turning the
    # option off (or leaving a partial file) between the reads is honored.
    loaded = config.load_project_config(root)
    if not isinstance(loaded, dict) or config.diagnostics_config(
            loaded.get("diagnostics"))["enabled"] is not True:
        return False
    # The file must still be the same settings path after both reads; if it
    # vanished, a parent's settings could have answered the second read.
    after = _settings_file(root)
    return after is not None and str(after) == str(settings)


def enabled(decision) -> bool:
    """Whether a new detailed diagnostic may be recorded for ``decision``."""
    try:
        root = _consenting_root(decision)
        if root is None:
            return False
        return _project_opted_in(root) is True
    except Exception:  # noqa: BLE001 - consent fails closed on anything
        return False


# ------------------------------------------------------------------ capture

def _format(exc, warning: bool) -> tuple[str, str] | None:
    """(kind, text): an exception chain, or the caller's origin stack. No locals."""
    if exc is not None and isinstance(exc, BaseException):
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__,
                                           limit=STACK_LIMIT)
        return "exception", "".join(lines)
    if warning:
        # Drop this module's own frames (capture/_format) from the origin stack.
        frames = [frame for frame in traceback.extract_stack(limit=STACK_LIMIT + 4)
                  if os.path.abspath(frame.filename) != os.path.abspath(__file__)]
        return "origin_stack", ("Origin stack (most recent call last):\n"
                                + "".join(traceback.format_list(frames[-STACK_LIMIT:])))
    return None


def _bounded(text: str) -> tuple[bytes, bool]:
    """UTF-8 bytes of at most TEXT_LIMIT, cut on a character boundary."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= TEXT_LIMIT:
        return raw, False
    return raw[:TEXT_LIMIT].decode("utf-8", "ignore").encode("utf-8"), True


def capture(decision, *, event, reason, exc=None, warning=False) -> str | None:
    """Record one detailed diagnostic; return its 32-hex ID, or None.

    None when consent is off (checked before formatting and again right before
    the first write), nothing is capturable, or any write fails. Never raises
    and never alters the caller's outcome.
    """
    try:
        if enabled(decision) is not True:
            return None
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
        if enabled(decision) is not True:
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
