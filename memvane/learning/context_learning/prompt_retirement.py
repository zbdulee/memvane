"""One-time deletion of retired prompt cases in a default learning namespace.

Prompt cases are no longer captured (design r3 D, decision 10). The first
1.0.18 worker pass of a default namespace deletes, in its shared queue, the
``cases`` rows whose sample is a prompt case of schema 4 or lower, and the
private ``samples`` rows of the same request ids. Nothing else changes: edit
(v5) cases, cohort exposure, families and every other table stay. A marker
in the namespace makes later passes do nothing; the count goes to the marker
and to one ``learning.migration`` event.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

MARKER = 'prompt-cases-retired.json'
SCHEMA = 'memvane-prompt-cases-retired-v1'


def _retired(sample_json: str) -> bool:
    try:
        sample = json.loads(sample_json)
        version = sample.get('schema_version')
        return (type(version) is int and version <= 4
                and sample.get('context_origin') == 'user_prompt')
    except (ValueError, TypeError, AttributeError):
        return False


def retire(root, project, config) -> dict:
    """Delete retired prompt cases once; ``{'status', 'count'}``. Raises on I/O errors."""
    from .dataset import read_private_json
    from .local_cycle import _atomic_json
    from . import shared
    root, project = Path(root), Path(project).resolve(strict=True)
    marker = root / MARKER
    if marker.exists() or marker.is_symlink():
        read_private_json(marker)  # an unsafe marker refuses, it is never replaced
        return {'status': 'already_retired', 'count': 0}
    database = root / shared.DATABASE_NAME
    ids = []
    if database.exists() or database.is_symlink():
        with shared._db(root) as db:
            ids = [rid for rid, sample in db.execute(
                'SELECT request_id,sample FROM cases WHERE project_key=?',
                (shared._project_key(project),)) if _retired(sample)]
    private = 0
    if ids:
        # Private rows first: a crash before the case delete leaves the case,
        # which the next pass deletes; never a private row without its case.
        from .seam import _private_state
        state = _private_state(config, project, create=False)
        if state is not None and (state / 'learning.sqlite3').is_file():
            from .store import database as private_database
            with private_database(state) as db:
                for rid in ids:
                    private += db.execute('DELETE FROM samples WHERE request_id=? AND schema_version<=4',
                                          (rid,)).rowcount
        with shared._db(root) as db:
            count = 0
            for rid in ids:
                count += db.execute('DELETE FROM cases WHERE project_key=? AND request_id=?',
                                    (shared._project_key(project), rid)).rowcount
    else:
        count = 0
    _atomic_json(marker, {'schema': SCHEMA, 'count': count, 'privateSamples': private,
                          'retiredAtUnixMs': int(time.time() * 1000)})
    _record(project, count, private)
    return {'status': 'retired', 'count': count}


def _record(project, count, private) -> None:
    try:
        from memvane.journal.api import select_route
        from memvane.diagnostics.api import emit
        decision = select_route(str(project), action='update')
        emit('learning.migration', outcome='prompt_cases_deleted', reason='prompt_capture_retired',
             decision=decision, count=count, privateSamples=private)
    except Exception:
        pass  # The marker keeps the count; the event is diagnostic only.


def retire_quietly(root, project, config) -> dict:
    """``retire`` for the worker pass: a failure is reported, never raised."""
    try:
        return retire(root, project, config)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return {'status': 'retirement_deferred', 'count': 0}
