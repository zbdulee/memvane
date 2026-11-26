"""Project-root access to the ledger's log rows.

Log rows (``audit_events``) are written best-effort: no generation, deletion or
resume decision reads them, so a failed append loses only a diagnostic line.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3

from memvane.wiki.pipeline.types import AUDIT_KINDS, PipelineError


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
