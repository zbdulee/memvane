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


def scoped_hits(rows, scope_root, config, collections, index_path):
    """Drop a malformed, foreign, or canonically out-of-scope QMD result."""
    if not isinstance(rows, list):
        return []
    scope = Path(scope_root).resolve(strict=True)
    resolved = resolve_paths.resolve_paths(str(scope), json.dumps(config))
    if resolved.get('refused'):
        return []
    bases = {entry['name']: (scope / entry['path']).resolve()
             for entry in resolved.get('indexEntries', [])
             if entry['name'] in collections}
    accepted = []
    try:
        with sqlite_read.connect(index_path, timeout=.2) as db:
            return _checked_hits(db, rows, bases)
    except (OSError, ValueError, sqlite3.Error):
        return []


def _checked_hits(db, rows, bases):
    accepted = []
    revisions = {}
    for hit in rows:
        if not isinstance(hit, dict):
            continue
        uri = hit.get('file')
        if not isinstance(uri, str) or not uri or len(uri) > 4096:
            continue
        body = uri[6:] if uri.startswith('qmd://') else uri
        collection, separator, relative = body.partition('/')
        if not separator or collection not in bases:
            continue
        if hit.get('_collection') not in (None, collection):
            continue
        parts = PurePosixPath(relative).parts
        if (not parts or relative.startswith('/') or '\\' in relative
                or any(part in ('', '.', '..') for part in relative.split('/'))):
            continue
        base = bases[collection]
        try:
            source = (base / relative).resolve(strict=True)
            source.relative_to(base)
        except (OSError, ValueError):
            continue
        if not source.is_file() or source.is_symlink():
            continue
        key = (collection, relative)
        if key not in revisions:
            indexed = db.execute('SELECT hash FROM documents WHERE collection=? AND path=? '
                                 'AND active=1', key).fetchall()
            if (len(indexed) != 1 or not isinstance(indexed[0][0], str)
                    or len(indexed[0][0]) != 64):
                revisions[key] = False
            else:
                digest = hashlib.sha256()
                try:
                    if source.stat().st_size > MAX_REVISION_BYTES:
                        revisions[key] = False
                        continue
                    size = 0
                    with source.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            size += len(block)
                            if size > MAX_REVISION_BYTES:
                                break
                            digest.update(block)
                    revisions[key] = (size <= MAX_REVISION_BYTES
                                      and digest.hexdigest() == indexed[0][0])
                except OSError:
                    revisions[key] = False
        if not revisions[key]:
            continue
        # QMD's hit body/title and private annotation keys are untrusted here.
        # Wiki metadata is rebuilt from the verified local file later; raw
        # results only expose a title derived from their own path.
        score = hit.get('score', 0)
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            continue
        clean = {'file': f'qmd://{collection}/{relative}',
                 '_collection': collection, 'title': source.stem,
                 'score': float(score)}
        line = hit.get('line')
        if type(line) is int and 1 <= line <= 100000:
            clean['line'] = line
        accepted.append(clean)
    return accepted
