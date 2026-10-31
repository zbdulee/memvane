#!/usr/bin/env python3
"""Per-source-revision state of the wiki card budget (correctness ledgers).

Two files under the project compile directory, both keyed by a source
revision ``(path, collection, sha256)``:

- ``budget-refused.jsonl``: a revision whose extraction ended with a card over
  the selector budget after its one split attempt. The compile worker never
  extracts that revision again; a source edit (new sha256) or
  ``forceRecompile`` compiles it again.
- ``budget-stash.json``: a revision whose split or measurement could not run
  yet (run extractor budget exhausted, ``resource_deferred``, tokenizer runtime
  failure). It keeps the extractor's unfinished candidates so the next run
  resumes without paying for the extraction again, for at most
  ``MAX_RESUME_ATTEMPTS`` resumes.

These are correctness state, never trimmed like an audit log: every write
rewrites the file atomically, keeping the latest row per source and dropping
only rows whose source file is gone or now has other bytes (no future job can
match them). The compile worker writes them under its project run lock.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import compile_paths as cp
import wiki_compile as wc
import wiki_freshness

MAX_RESUME_ATTEMPTS = 3
MAX_STASH_BYTES = 4 * 1024 * 1024


def _key(revision: dict) -> tuple:
    return (revision.get('path'), revision.get('collection'), revision.get('sha256'))


def _source_key(row: dict) -> tuple:
    return (row.get('path'), row.get('collection'))


def _current_sha(root: Path, rel) -> str | None:
    if not isinstance(rel, str) or not rel:
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    snapshot = wiki_freshness.snapshot_bytes(path)
    return snapshot[0].get('sha256') if snapshot is not None else None


def _live(root: Path, rows: list[dict], keep: dict | None = None) -> list[dict]:
    """Latest row per source whose sha256 still matches the source file."""
    latest: dict[tuple, dict] = {}
    for row in rows:
        if isinstance(row, dict):
            latest[_source_key(row)] = row
    if keep is not None:
        latest[_source_key(keep)] = keep
    shas: dict = {}
    out = []
    for source, row in latest.items():
        if row is keep:
            out.append(row)
            continue
        if source not in shas:
            shas[source] = _current_sha(root, source[0])
        if shas[source] is not None and shas[source] == row.get('sha256'):
            out.append(row)
    return out


def refused_path(root: Path) -> Path | None:
    return cp.ledger(root, cp.BUDGET_REFUSED)


def stash_path(root: Path) -> Path | None:
    return cp.ledger(root, cp.BUDGET_STASH)


def writable(root: Path) -> bool:
    """Both files can be written; checked before any paid extractor call."""
    paths = (refused_path(root), stash_path(root))
    return all(path is not None and wc.ledger_writable(path) for path in paths)


def find_refused(root: Path, revision: dict) -> dict | None:
    path = refused_path(root)
    if path is None or not path.is_file():
        return None
    key = _key(revision)
    for row in reversed(wc.read_jsonl(path)):
        if _key(row) == key:
            return row
    return None


def record_refused(root: Path, revision: dict, row: dict) -> bool:
    """Make ``revision`` terminal. False when the ledger could not be rewritten."""
    path = refused_path(root)
    if path is None or (path.exists() and not path.is_file()):
        return False
    entry = {**row, 'path': revision.get('path'), 'collection': revision.get('collection'),
             'sha256': revision.get('sha256')}
    rows = _live(Path(root), wc.read_jsonl(path) if path.exists() else [], keep=entry)
    text = ''.join(json.dumps(item, ensure_ascii=False, sort_keys=True) + '\n' for item in rows)
    return wc.write_text_atomic(path, text)


def _read_stash(path: Path) -> list[dict]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_STASH_BYTES:
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return []
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def load_stash(root: Path, revision: dict) -> dict | None:
    path = stash_path(root)
    if path is None:
        return None
    key = _key(revision)
    for row in _read_stash(path):
        if _key(row) == key and isinstance(row.get('candidates'), list):
            return row
    return None


def save_stash(root: Path, revision: dict, entry: dict | None) -> bool:
    """Replace (or with ``None`` drop) the stash of ``revision``; atomic."""
    path = stash_path(root)
    if path is None or (path.exists() and not path.is_file()):
        return False
    key = _key(revision)
    rows = [row for row in _read_stash(path) if _key(row) != key]
    keep = None
    if entry is not None:
        keep = {**entry, 'path': revision.get('path'),
                'collection': revision.get('collection'), 'sha256': revision.get('sha256')}
    rows = _live(Path(root), rows, keep=keep)
    if not rows and not path.exists():
        return True
    text = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    if len(text.encode('utf-8')) > MAX_STASH_BYTES:
        return False
    return wc.write_text_atomic(path, text + '\n')
