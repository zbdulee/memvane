"""One-time migration from 1.x JSONL/handoff artifacts into ``wiki-pipeline.sqlite3``."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

from memvane.wiki.pipeline.store import open_pipeline, pipeline_db_path
from memvane.wiki.pipeline.types import FENCE_GLOBAL_PUBLISH

_AUDIT_FILE_MAP = (
    ('generated-manifest.jsonl', 'manifest'),
    ('tombstones.jsonl', 'tombstone'),
    ('candidates.jsonl', 'candidate'),
    ('merge-needed.jsonl', 'merge_needed'),
    ('discard-ledger.jsonl', 'discard'),
    ('verify-log.jsonl', 'verify_log'),
    ('verify-deleted.jsonl', 'verify_deleted'),
    ('verify-skipped.jsonl', 'verify_skipped'),
    ('dedup-needed.jsonl', 'dedup_needed'),
    ('dedup-skipped.jsonl', 'dedup_skipped'),
    ('dedup-deleted.jsonl', 'dedup_deleted'),
)

_COOLDOWN_FILES = (
    ('cooldown', 'compile'),
    ('verify-cooldown', 'verify'),
    ('verify-engine-cooldown.json', 'verify_engine'),
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _legacy_files(compile_dir: Path) -> list[Path]:
    found: list[Path] = []
    if not compile_dir.is_dir():
        return found
    fixed = (
        'source-queue.jsonl',
        'verify-queue.jsonl',
        'source-refresh-pending.jsonl',
        'source-refresh-settled.json',
        'generated-manifest.jsonl.compact-stamp',
        *(name for name, _ in _AUDIT_FILE_MAP),
        *(name for name, _ in _COOLDOWN_FILES),
    )
    for name in fixed:
        candidate = compile_dir / name
        if candidate.exists():
            found.append(candidate)
    for pattern in ('*.claimed.*', 'dedup-judge-*'):
        found.extend(p for p in compile_dir.glob(pattern) if p.is_file())
    handoff = compile_dir / '.handoff' / 'v1'
    if handoff.exists():
        found.append(handoff)
    return found


def _hidden_wiki_files(wiki_root: Path | None) -> list[Path]:
    if wiki_root is None or not Path(wiki_root).is_dir():
        return []
    found: list[Path] = []
    for path in Path(wiki_root).rglob('*'):
        if not path.is_file():
            continue
        name = path.name
        if (name.startswith('.first-') or '.pending-' in name
                or name == '.pending-ownership.json' or name.startswith('.verify-swap-')):
            found.append(path)
    return found


def needs_v1_migration(project_root: Path | str, *, wiki_root: Path | str | None = None) -> bool:
    """Return True when 1.x compile/handoff artifacts exist and migration has not completed."""
    root = Path(project_root)
    db_path = pipeline_db_path(root)
    if db_path.exists():
        try:
            with open_pipeline(root, readonly=True) as store:
                if store.meta_get('migrated_from_v1_at') is not None:
                    return False
        except Exception:
            pass
    compile_dir = root / '.memvane/compile'
    if _legacy_files(compile_dir):
        return True
    wiki = Path(wiki_root) if wiki_root is not None else (root / '.memvane/wiki')
    return bool(_hidden_wiki_files(wiki))


def migrate_from_v1(project_root: Path | str, *, wiki_root: Path | str | None = None,
                    remove_legacy: bool = True) -> dict:
    """Idempotently migrate 1.x compile/verify/refresh/handoff state into ``wiki-pipeline.sqlite3``."""
    root = Path(project_root)
    compile_dir = root / '.memvane/compile'
    wiki = Path(wiki_root) if wiki_root is not None else (root / '.memvane/wiki')
    counts = {'compileJobs': 0, 'verifyJobs': 0, 'refreshes': 0,
              'audits': 0, 'cooldowns': 0, 'staged': 0, 'removed': 0}

    with open_pipeline(root) as store:
        if store.meta_get('migrated_from_v1_at') is not None:
            if remove_legacy:
                counts['removed'] = _cleanup_legacy(compile_dir, wiki)
            return {'alreadyMigrated': True, **counts}

        _migrate_queues(store, compile_dir, counts)
        _migrate_refreshes(store, compile_dir, counts)
        _migrate_audits(store, compile_dir, counts)
        _migrate_cooldowns(store, compile_dir, counts)
        _migrate_handoffs_and_hidden(store, compile_dir, wiki, counts)
        store.meta_set('migrated_from_v1_at', str(time.time()))

    if remove_legacy:
        counts['removed'] = _cleanup_legacy(compile_dir, wiki)
    return {'alreadyMigrated': False, **counts}


def _migrate_queues(store, compile_dir: Path, counts: dict) -> None:
    src_files = [compile_dir / 'source-queue.jsonl', *sorted(compile_dir.glob('source-queue.jsonl.claimed.*'))]
    for path in src_files:
        for row in _read_jsonl(path):
            source = row.get('source') if isinstance(row.get('source'), dict) else {}
            col = str(source.get('collection') or '')
            rel = str(source.get('path') or '')
            if not rel:
                continue
            store.enqueue_compile_job(
                uuid.uuid4().hex, col, rel,
                str(row.get('trigger') or 'post_tool_source'),
                str(row.get('engine') or 'unknown'), row, replace_queued=False,
            )
            counts['compileJobs'] += 1
    ver_files = [compile_dir / 'verify-queue.jsonl', *sorted(compile_dir.glob('verify-queue.jsonl.claimed.*'))]
    for path in ver_files:
        for row in _read_jsonl(path):
            target = str(row.get('targetPath') or '')
            if not target:
                continue
            store.enqueue_verify_job(uuid.uuid4().hex, target, row.get('sourceHash'), row)
            counts['verifyJobs'] += 1


def _migrate_refreshes(store, compile_dir: Path, counts: dict) -> None:
    settled_path = compile_dir / 'source-refresh-settled.json'
    settled_map = {}
    if settled_path.is_file():
        try:
            doc = json.loads(settled_path.read_text(encoding='utf-8'))
            if isinstance(doc, dict) and isinstance(doc.get('sources'), dict):
                settled_map = doc['sources']
        except ValueError:
            settled_map = {}
    for path in (compile_dir / 'source-refresh-pending.jsonl', compile_dir / '.handoff/v1/refresh/events.jsonl'):
        for row in _read_jsonl(path):
            eid = row.get('eventId')
            src = row.get('sourcePath')
            if not isinstance(eid, str) or not isinstance(src, str):
                continue
            store.record_refresh(eid, src, str(row.get('ts') or ''), str(row.get('engine') or 'unknown'))
            counts['refreshes'] += 1
            settled = settled_map.get(src)
            if isinstance(settled, dict) and settled.get('eventId') == eid and settled.get('sha256'):
                store.settle_refresh(eid, str(settled['sha256']), str(settled.get('reason') or 'settled'), time.time())


def _migrate_audits(store, compile_dir: Path, counts: dict) -> None:
    for filename, kind in _AUDIT_FILE_MAP:
        for row in _read_jsonl(compile_dir / filename):
            store.append_audit(
                kind, row,
                target_path=row.get('targetPath'),
                source_path=row.get('sourcePath'),
                body_hash=row.get('bodyHash') or row.get('sourceBodyHash'),
            )
            counts['audits'] += 1


def _migrate_cooldowns(store, compile_dir: Path, counts: dict) -> None:
    for filename, scope in _COOLDOWN_FILES:
        path = compile_dir / filename
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding='utf-8').strip()
            data = json.loads(raw) if raw.startswith('{') else {'until': float(raw)}
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and 'until' in data:
            store.set_cooldown(scope, 'default', float(data['until']), data)
            counts['cooldowns'] += 1
        elif isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, (int, float)):
                    store.set_cooldown(scope, str(key), float(val), {'until': float(val)})
                    counts['cooldowns'] += 1
    for path in sorted(compile_dir.glob('dedup-judge-*')):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                store.set_cooldown('dedup_judge', path.name, float(data.get('until', 0.0)), data)
                counts['cooldowns'] += 1
        except (OSError, ValueError):
            continue


def _migrate_handoffs_and_hidden(store, compile_dir: Path, wiki: Path, counts: dict) -> None:
    hold_dir = compile_dir / '.handoff/v1/hold'
    if hold_dir.is_dir():
        for entry in sorted(hold_dir.iterdir()):
            if not entry.is_dir():
                continue
            raw_file = entry / 'raw'
            img_file = entry / 'image'
            if not raw_file.is_file():
                store.set_fence(FENCE_GLOBAL_PUBLISH, 'opaque_handoff_migrated', {'id': entry.name})
                continue
            try:
                raw_meta = json.loads(raw_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                store.set_fence(FENCE_GLOBAL_PUBLISH, 'opaque_handoff_migrated', {'id': entry.name})
                continue
            target = str(raw_meta.get('targetPath') or '')
            if not target:
                store.set_fence(FENCE_GLOBAL_PUBLISH, 'opaque_handoff_migrated', {'id': entry.name})
                continue
            blob = img_file.read_bytes() if img_file.is_file() else b''
            img_sha = hashlib.sha256(blob).hexdigest()
            if store.stage_card(
                entry.name, target, 'hold', 'held', blob, img_sha,
                raw_meta, {'migrated': True}, expected_target_sha=raw_meta.get('expectedTargetHash'),
            ) is not None:
                counts['staged'] += 1

    for hidden in _hidden_wiki_files(wiki):
        rel = hidden.relative_to(wiki).as_posix()
        store.append_audit('discard', {'migratedHiddenFile': rel}, target_path=rel)
        counts['audits'] += 1


def _cleanup_legacy(compile_dir: Path, wiki: Path) -> int:
    removed = 0
    for path in _legacy_files(compile_dir):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        elif path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    handoff_root = compile_dir / '.handoff'
    if handoff_root.is_dir():
        try:
            if not any(handoff_root.iterdir()):
                handoff_root.rmdir()
        except OSError:
            pass
    for hidden in _hidden_wiki_files(wiki):
        hidden.unlink(missing_ok=True)
        removed += 1
    return removed
