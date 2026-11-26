"""Project-root access to the ledger's log, suppression and cooldown rows.

Log rows (``audit_events``) are written best-effort: no generation, deletion or
resume decision reads them, so a failed append loses only a diagnostic line.
Suppression and cooldown writes return whether a row was recorded; their
callers surface a False because a missing row means another paid call.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import time

from memvane.kernel import cooldown as kernel_cooldown
from memvane.wiki.pipeline.types import (AUDIT_KINDS, COOLDOWN_SCOPES, GLOBAL_COOLDOWN_KEY, PipelineError,
                                         UNATTRIBUTED_COOLDOWN_KEY)


def _text(value):
    return value if isinstance(value, str) and value else None


def _source_path(payload: dict) -> str | None:
    source = payload.get('source')
    if isinstance(source, dict) and _text(source.get('path')):
        return source['path']
    return _text(payload.get('sourcePath'))


class AuditLog:
    """Append-only log sink for one ``audit_events`` kind under a project root."""

    def __init__(self, project_root: Path | str, kind: str) -> None:
        if kind not in AUDIT_KINDS:
            raise PipelineError(f'invalid_audit_kind:{kind}')
        self.root = Path(project_root)
        self.kind = kind

    def append(self, payload: dict) -> bool:
        from memvane.wiki.pipeline.store import open_pipeline
        try:
            with open_pipeline(self.root) as store:
                store.append_audit(self.kind, payload, target_path=_text(payload.get('targetPath')),
                                   source_path=_source_path(payload),
                                   body_hash=_text(payload.get('sourceHash')))
            return True
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False


def log_rows(project_root: Path | str, kind: str) -> list[dict]:
    """Every logged payload of ``kind`` in write order; empty without a ledger."""
    from memvane.wiki.pipeline.store import open_pipeline
    try:
        with open_pipeline(project_root, readonly=True) as store:
            return [row['payload'] for row in store.query_audit(kind)]
    except FileNotFoundError:
        return []


def suppressions(project_root: Path | str, kind: str) -> dict[str, str]:
    """``key -> body hash`` of one suppression kind; empty without a ledger."""
    from memvane.wiki.pipeline.store import open_pipeline
    try:
        with open_pipeline(project_root, readonly=True) as store:
            return store.suppressions(kind)
    except FileNotFoundError:
        return {}


def set_suppression(project_root: Path | str, kind: str, key: str, body_hash: str, payload: dict) -> bool:
    """Record a paid-call suppression; False when the ledger refused the write."""
    from memvane.wiki.pipeline.store import open_pipeline
    try:
        with open_pipeline(project_root) as store:
            store.set_suppression(kind, key, body_hash, payload)
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


def cooling_keys(project_root: Path | str, scope: str) -> set[str]:
    """Keys of ``scope`` whose cooldown is still active.

    Unreadable state fails open (empty set): the worst case is one more attempt.
    An expiry beyond the ceiling is not active (``kernel.cooldown.expiry_active``).
    """
    from memvane.wiki.pipeline.store import open_pipeline
    if scope not in COOLDOWN_SCOPES:
        raise PipelineError(f'invalid_cooldown_scope:{scope}')
    try:
        with open_pipeline(project_root, readonly=True) as store:
            rows = store.cooldowns(scope)
    except (OSError, ValueError, sqlite3.Error):
        return set()
    now = time.time()
    return {key for key, until in rows.items() if kernel_cooldown.expiry_active(until, now)}


def cooldown_active(project_root: Path | str, scope: str, key: str = GLOBAL_COOLDOWN_KEY) -> bool:
    return key in cooling_keys(project_root, scope)


def set_cooldown(project_root: Path | str, scope: str, seconds, key: str = GLOBAL_COOLDOWN_KEY) -> bool:
    """Pause ``key`` of ``scope`` for ``seconds`` (clamped); False when nothing was recorded."""
    from memvane.wiki.pipeline.store import open_pipeline
    if scope not in COOLDOWN_SCOPES:
        raise PipelineError(f'invalid_cooldown_scope:{scope}')
    now = time.time()
    try:
        with open_pipeline(project_root) as store:
            store.prune_cooldowns(scope, now)
            store.set_cooldown(scope, key or UNATTRIBUTED_COOLDOWN_KEY, kernel_cooldown.expiry_value(now, seconds))
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False
