"""Verify QMD hits before reading bodies or offering learning candidates."""
from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path, PurePosixPath
import sqlite3

import resolve_paths
import sqlite_read

MAX_REVISION_BYTES = 2 * 1024 * 1024
# QMD CLI rows are already capped at 100. This bound keeps a malformed or
# fixture over-return from turning one prompt into an unbounded hash scan.
MAX_CHECKED_ROWS = 256


def _count(drops, reason, amount=1):
    if drops is not None and amount:
        drops[reason] = drops.get(reason, 0) + amount


def scoped_hits(rows, scope_root, config, collections, index_path, drops=None,
                with_revision=False):
    """Drop a malformed, foreign, stale, or canonically out-of-scope QMD result.

    `drops`, when given, receives one objective reason count per dropped row.
    Only returned rows are examined; the project is never scanned.
    `with_revision` adds the private `_verified_path`/`_verified_revision`
    of the exact indexed bytes just checked. Recall uses the verified path
    for the selected-file log; neither value authorizes raw-body injection.
    """
    if not isinstance(rows, list):
        return []
    if len(rows) > MAX_CHECKED_ROWS:
        _count(drops, 'row_limit', len(rows) - MAX_CHECKED_ROWS)
        rows = rows[:MAX_CHECKED_ROWS]
    try:
        scope = Path(scope_root).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        _count(drops, 'scope_unavailable', len(rows))
        return []
    resolved = resolve_paths.resolve_paths(str(scope), json.dumps(config))
    if resolved.get('refused'):
        _count(drops, 'scope_refused', len(rows))
        return []
    bases = {entry['name']: (scope / entry['path']).resolve()
             for entry in resolved.get('indexEntries', [])
             if entry['name'] in collections}
    local = {} if drops is not None else None
    try:
        with sqlite_read.connect(index_path, timeout=.2) as db:
            accepted = _checked_hits(db, rows, bases, local, with_revision)
    except (OSError, ValueError, sqlite3.Error):
        # An unreadable or schema-mismatched index proves nothing fresh.
        _count(drops, 'index_unavailable', len(rows))
        return []
    for reason, amount in (local or {}).items():
        _count(drops, reason, amount)
    return accepted


def _checked_hits(db, rows, bases, drops=None, with_revision=False):
    accepted = []
    revisions = {}
    verified = {}
    for hit in rows:
        if not isinstance(hit, dict):
            _count(drops, 'malformed')
            continue
        uri = hit.get('file')
        if not isinstance(uri, str) or not uri or len(uri) > 4096:
            _count(drops, 'malformed')
            continue
        body = uri[6:] if uri.startswith('qmd://') else uri
        collection, separator, relative = body.partition('/')
        if not separator or collection not in bases:
            _count(drops, 'foreign_collection')
            continue
        if hit.get('_collection') not in (None, collection):
            _count(drops, 'foreign_collection')
            continue
        parts = PurePosixPath(relative).parts
        if (not parts or relative.startswith('/') or '\\' in relative
                or any(part in ('', '.', '..') for part in relative.split('/'))):
            _count(drops, 'unsafe_path')
            continue
        base = bases[collection]
        try:
            # Keep the lexical path long enough to reject links inside the
            # collection. Checking only the resolved target misses links that
            # point to another file within the same collection.
            lexical = base
            for part in parts:
                lexical = lexical / part
                if lexical.is_symlink():
                    raise ValueError('symlink in result path')
            source = lexical.resolve(strict=True)
            source.relative_to(base)
        except FileNotFoundError:
            _count(drops, 'missing')
            continue
        except (OSError, ValueError):
            # Resolves outside the collection (for example via a symlink).
            _count(drops, 'out_of_scope')
            continue
        if not source.is_file() or source.is_symlink():
            _count(drops, 'missing')
            continue
        key = (collection, relative)
        if key not in revisions:
            indexed = db.execute('SELECT hash FROM documents WHERE collection=? AND path=? '
                                 'AND active=1', key).fetchall()
            if (len(indexed) != 1 or not isinstance(indexed[0][0], str)
                    or len(indexed[0][0]) != 64):
                revisions[key] = 'not_indexed'
            else:
                digest = hashlib.sha256()
                try:
                    if source.stat().st_size > MAX_REVISION_BYTES:
                        revisions[key] = 'oversize'
                    else:
                        size = 0
                        with source.open('rb') as stream:
                            for block in iter(lambda: stream.read(1024 * 1024), b''):
                                size += len(block)
                                if size > MAX_REVISION_BYTES:
                                    break
                                digest.update(block)
                        if size > MAX_REVISION_BYTES:
                            revisions[key] = 'oversize'
                        elif digest.hexdigest() == indexed[0][0]:
                            revisions[key] = True
                            verified[key] = (str(source), indexed[0][0])
                        else:
                            # Current bytes differ from the indexed revision:
                            # an edit, checkout, or pull the index has not seen.
                            revisions[key] = 'modified'
                except OSError:
                    revisions[key] = 'read_error'
        if revisions[key] is not True:
            _count(drops, revisions[key])
            continue
        # QMD's hit body/title and private annotation keys are untrusted here.
        # Wiki metadata is rebuilt from the verified local file later; raw
        # results only expose a title derived from their own path.
        score = hit.get('score', 0)
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            _count(drops, 'malformed')
            continue
        clean = {'file': f'qmd://{collection}/{relative}',
                 '_collection': collection, 'title': source.stem,
                 'score': float(score)}
        line = hit.get('line')
        if type(line) is int and 1 <= line <= 100000:
            clean['line'] = line
        if with_revision:
            clean['_verified_path'], clean['_verified_revision'] = verified[key]
        accepted.append(clean)
    return accepted
