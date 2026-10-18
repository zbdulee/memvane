"""Read-only QMD index adapter for file-sync baselines.

File detection asks for active document paths and revisions; it does not own
the QMD SQLite schema, selected route, or index connection.
"""
from __future__ import annotations

import os
from pathlib import Path

import qmd_route
import sqlite_read


def active_document_hashes(project_root, collections, *, fixture_env=None):
    """Return {collection: [(relative_path, hash), ...]} from the selected DB.

    A missing/unsafe route or unreadable index raises; callers must preserve
    their pending change rather than interpreting that failure as a deletion.
    """
    if fixture_env is None:
        fixture_env = os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1'
    route = qmd_route.project_paths(project_root, isolated=True,
                                    reject_conflicts=True, fixture_env=fixture_env)
    result = {}
    with sqlite_read.connect(route['INDEX_PATH']) as db:
        for name in collections:
            rows = []
            for rel, digest in db.execute(
                    'SELECT path,hash FROM documents WHERE collection=? AND active=1',
                    (name,)):
                if (not isinstance(rel, str) or Path(rel).is_absolute()
                        or '..' in Path(rel).parts
                        or rel.startswith('.auto-context/file-changes/')):
                    continue
                rows.append((rel, digest))
            result[name] = rows
    return result
