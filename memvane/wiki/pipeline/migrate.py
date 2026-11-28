"""One-time 1.x -> 2.0 cutover of a project's wiki compile state.

The decision ledgers come across: ``generated-manifest.jsonl`` (machine
retirement of generated cards when their source is deleted) and the paid-call
suppressions ``verify-skipped.jsonl`` / ``dedup-skipped.jsonl``. Unfinished 1.x
review rows (``verify-queue.jsonl`` and its ``.claimed.*`` files) come across only
as the ``kind: review`` payloads the caller's ``reviews`` filter returns for them:
a generated card is otherwise never queued again while its source stays the
same. Every other 1.x in-progress artifact is discarded: a source change or a
full-scan sync recreates the work. The import and its marker commit in one transaction; the
1.x files are removed only after that commit, so a crash at any point either
leaves everything to import again or leaves only removal to repeat.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Callable

from memvane.wiki.pipeline.store import open_pipeline, pipeline_db_path

MARKER = 'migrated_from_v1_at'

_IMPORTED = ('generated-manifest.jsonl', 'verify-skipped.jsonl', 'dedup-skipped.jsonl')
_DISCARDED = (
    'source-queue.jsonl', 'verify-queue.jsonl', 'source-refresh-pending.jsonl',
    'source-refresh-pending.jsonl.lock', 'source-refresh-settled.json', 'source-refresh-settled.json.lock',
    'generated-manifest.jsonl.compact-stamp', 'tombstones.jsonl', 'candidates.jsonl', 'merge-needed.jsonl',
    'verify-log.jsonl', 'verify-deleted.jsonl', 'dedup-needed.jsonl', 'dedup-deleted.jsonl',
    'discard-ledger.jsonl', 'cooldown', 'verify-cooldown', 'verify-engine-cooldown.json',
    'dedup-judge-cooldown', 'dedup-judge-engine-cooldown.json',
)
_DISCARDED_GLOBS = ('*.claimed.*', '*.jsonl.lock', 'dedup-judge-*')
# 1.1.4 files that share a discarded pattern.
_KEPT = ('edit-inbox.jsonl.lock',)


def _lines(path: Path) -> list[tuple[int, bytes]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return [(number, line) for number, line in enumerate(data.splitlines(), 1) if line.strip()]


def _rows(path: Path) -> list[tuple[int, bytes, dict]]:
    rows = []
    for number, line in _lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A torn or foreign line carries no decision.
        if isinstance(row, dict):
            rows.append((number, line, row))
    return rows


def _manifest(compile_dir: Path) -> list[tuple[str, dict]]:
    return [(f'v1-manifest:{number}:{hashlib.sha256(line).hexdigest()}', row)
            for number, line, row in _rows(compile_dir / 'generated-manifest.jsonl')]


def _verify_skips(compile_dir: Path) -> list[dict]:
    items = []
    for _, _, row in _rows(compile_dir / 'verify-skipped.jsonl'):
        source, body = row.get('sourcePath'), row.get('sourceBodyHash')
        if isinstance(source, str) and source and isinstance(body, str) and body:
            items.append({'key': source, 'bodyHash': body, 'row': row})
    return items


def v1_review_rows(compile_dir: Path) -> list[dict]:
    """Unfinished 1.x verify rows: the queue file, then each claimed file in name order."""
    paths = [compile_dir / 'verify-queue.jsonl',
             *sorted(p for p in compile_dir.glob('verify-queue.jsonl.claimed.*') if p.is_file())]
    return [row for path in paths for _, _, row in _rows(path)]


def _dedup_skips(compile_dir: Path) -> list[dict]:
    items = []
    for _, _, row in _rows(compile_dir / 'dedup-skipped.jsonl'):
        values = [row.get(name) for name in ('pageA', 'pageB', 'pageAHash', 'pageBHash')]
        if not all(isinstance(v, str) and v for v in values):
            continue
        page_a, page_b, hash_a, hash_b = values
        if page_b < page_a:
            page_a, page_b, hash_a, hash_b = page_b, page_a, hash_b, hash_a
        items.append({'key': page_a + '\n' + page_b, 'bodyHash': hash_a + ':' + hash_b, 'row': row})
    return items


def _hidden_wiki_files(wiki_root: Path | None) -> list[Path]:
    if wiki_root is None or not Path(wiki_root).is_dir() or Path(wiki_root).is_symlink():
        return []
    found = []
    for path in Path(wiki_root).rglob('.*'):
        if path.is_symlink() or not path.is_file():
            continue
        name = path.name
        if (name.startswith('.first-') or name.startswith('.pc-') or name.startswith('.verify-swap-')
                or name == '.pending-ownership.json' or '.pending-' in name):
            found.append(path)
    return found


def _legacy_paths(compile_dir: Path) -> list[Path]:
    if not compile_dir.is_dir():
        return []
    found = [compile_dir / name for name in (*_IMPORTED, *_DISCARDED) if (compile_dir / name).exists()]
    for pattern in _DISCARDED_GLOBS:
        found.extend(p for p in compile_dir.glob(pattern)
                     if p.is_file() and p not in found and p.name not in _KEPT)
    handoff = compile_dir / '.handoff'
    if handoff.exists() or handoff.is_symlink():
        found.append(handoff)
    return found


def _wiki_root(root: Path, wiki_root: Path | str | None) -> Path:
    return Path(wiki_root) if wiki_root is not None else root / '.memvane/wiki'


def needs_v1_migration(project_root: Path | str, *, wiki_root: Path | str | None = None) -> bool:
    """True while any 1.x artifact remains; a project without one never waits.

    After the marker, the 1.x compile files are removed last (``_remove_legacy``), so their
    absence proves the hidden wiki files are gone too and the wiki walk is skipped.
    """
    root = Path(project_root)
    compile_dir = root / '.memvane/compile'
    if _legacy_paths(compile_dir):
        return True
    if _marked(root):
        return False
    return bool(_hidden_wiki_files(_wiki_root(root, wiki_root)))


def v1_migration_marked(project_root: Path | str) -> bool:
    return _marked(Path(project_root))


def _marked(root: Path) -> bool:
    if not pipeline_db_path(root).exists():
        return False
    with open_pipeline(root, readonly=True) as store:
        return store.meta_get(MARKER) is not None


def migrate_from_v1(project_root: Path | str, *, wiki_root: Path | str | None = None,
                    reviews: Callable[[list[dict]], list[dict]] | None = None) -> dict:
    """Import the decision ledgers, commit the marker, then remove every 1.x artifact.

    Callers hold ``.compile-run.lock`` and the card/wiki locks, so no 1.1.4 writer runs
    concurrently and an in-flight 1.x worker has already released the run lock.
    ``reviews`` maps the unfinished 1.x verify rows to the review payloads queued with
    the marker; without it those rows are discarded.
    """
    root = Path(project_root)
    compile_dir = root / '.memvane/compile'
    wiki = _wiki_root(root, wiki_root)
    if reviews is not None and not v1_migration_marked(root):
        payloads = reviews(v1_review_rows(compile_dir))
    else:
        payloads = []
    with open_pipeline(root) as store:
        counts = store.import_v1_ledgers(
            _manifest(compile_dir), _verify_skips(compile_dir), _dedup_skips(compile_dir), MARKER,
            reviews=payloads)
    counts['removed'] = _remove_legacy(compile_dir, wiki)
    return counts


def _remove_legacy(compile_dir: Path, wiki: Path) -> int:
    # Hidden wiki files first: a remaining 1.x compile file then marks unfinished removal.
    removed = 0
    for hidden in _hidden_wiki_files(wiki):
        hidden.unlink(missing_ok=True)
        removed += 1
    for path in _legacy_paths(compile_dir):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        removed += 1
    return removed
