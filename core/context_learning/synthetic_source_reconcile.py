"""Local-only SessionStart reconciliation for the one approved synthetic story.

This never calls a model. A shell edit has no host receipt and therefore does
not establish synthetic origin for its new bytes. It is durably recorded and
its stale generated cards are archived, while future external regeneration
waits for a separately provable source revision. Historical bytes survive.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time

from .safe_source import snapshot_beneath
from .shared import _db
from .synthetic_scope import COLLECTION_PATHS

MAX_FILES = 2000
MAX_CARDS = 5000
RAW_BASES = tuple(base for base in COLLECTION_PATHS.values()
                  if base != '.auto-context/wiki')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _source_snapshot(story, relative):
    try:
        return _hash(snapshot_beneath(story, relative))
    except FileNotFoundError:
        return None


def _files(story):
    found = {}
    for base in RAW_BASES:
        folder = story / base
        if not folder.exists():
            continue
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError('synthetic_reconcile_unsafe_root')
        for directory, dirs, files in os.walk(folder, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                        or name in dirs and not stat.S_ISDIR(info.st_mode)
                        or name in files and (not stat.S_ISREG(info.st_mode)
                                              or info.st_nlink != 1)):
                    raise ValueError('synthetic_reconcile_unsafe_entry')
            dirs[:] = [name for name in dirs if not name.startswith('.')]
            for name in files:
                if name.startswith('.') or not name.endswith('.md'):
                    continue
                path = Path(directory) / name
                relative = path.relative_to(story).as_posix()
                found[relative] = _source_snapshot(story, relative)
                if len(found) > MAX_FILES:
                    raise ValueError('synthetic_reconcile_file_limit')
    return found


def _ledger(db):
    latest = {}
    for kind, relative, sha in db.execute(
            'SELECT kind,relative_path,sha256 FROM experiment_generated ORDER BY entry_id'):
        latest[relative] = (kind, sha)
    from .source_card_provenance import latest_paths
    latest.update(latest_paths(db))
    return latest


def _detect(db, story, files):
    latest = _ledger(db)
    old = dict(db.execute('SELECT relative_path,sha256 FROM synthetic_source_inventory'))
    expected = {path: sha for path, (kind, sha) in latest.items()
                if any(path.startswith(base + '/') for base in RAW_BASES)
                and kind != 'writing_delete'}
    paths = sorted(set(files) | set(old) | set(expected))
    if len(paths) > MAX_FILES:
        raise ValueError('synthetic_reconcile_file_limit')
    detected = 0
    now = time.time()
    db.execute('BEGIN IMMEDIATE')
    for path in paths:
        before = old[path] if path in old else expected.get(path)
        after = files.get(path)
        if before != after:
            operation = 'create' if before is None else 'delete' if after is None else 'update'
            prior = db.execute('SELECT event_id FROM synthetic_source_events '
                               'WHERE relative_path=? ORDER BY rowid DESC LIMIT 1',
                               (path,)).fetchone()
            event_id = _hash(json.dumps([path, before, after,
                                         prior[0] if prior else None],
                                        separators=(',', ':')).encode())
            inserted = db.execute(
                'INSERT OR IGNORE INTO synthetic_source_events '
                '(event_id,relative_path,operation,before_sha256,after_sha256,state,recorded_at) '
                'VALUES (?,?,?,?,?,?,?)',
                (event_id, path, operation, before, after, 'pending', now)).rowcount
            detected += inserted
        db.execute('INSERT INTO synthetic_source_inventory VALUES (?,?,?) '
                   'ON CONFLICT(relative_path) DO UPDATE SET sha256=excluded.sha256, '
                   'recorded_at=excluded.recorded_at', (path, after, now))
    return detected


def _referencing_cards(story, source_path, before_sha):
    from wiki_card import frontmatter_source_revisions
    wiki = story / '.auto-context/wiki'
    if wiki.is_symlink() or not wiki.is_dir():
        raise ValueError('synthetic_reconcile_unsafe_wiki')
    target = story.name + '/' + source_path
    matches = []
    for count, card in enumerate(sorted(wiki.rglob('*.md')), start=1):
        if count > MAX_CARDS or card.is_symlink() or not card.is_file():
            raise ValueError('synthetic_reconcile_unsafe_card')
        relative = card.relative_to(story).as_posix()
        raw = snapshot_beneath(story, relative)
        content = raw.decode('utf8')
        end = content.find('\n---', 3)
        if not content.startswith('---\n') or end < 0:
            continue
        block = content[3:end]
        refs = frontmatter_source_revisions(block)
        if any(ref.get('path') == target and ref.get('sha256') == before_sha
               for ref in refs):
            matches.append((relative, _hash(raw), refs))
    return matches


def _archive_dir(root):
    folder = root / 'source-retired'
    if folder.is_symlink():
        raise ValueError('synthetic_reconcile_unsafe_archive')
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('synthetic_reconcile_unsafe_archive')
    return folder


def _retire_card(root, story, event_id, relative, sha, refs, latest):
    # Only an exact model-generated card may move. User-edited or unrelated
    # pages remain for review; no historical file is permanently deleted.
    if latest.get(relative) != sha or not relative.startswith('.auto-context/wiki/'):
        return False
    archive_rel = 'source-retired/' + _hash(relative.encode())[:24] + '-' + sha + '.md'
    archive = _archive_dir(root) / Path(archive_rel).name
    current = story / relative
    with _db(root) as db:
        old = db.execute('SELECT sha256,archive_relative,state FROM synthetic_retired_cards '
                         'WHERE relative_path=? AND sha256=?', (relative, sha)).fetchone()
        if old and old[:2] != (sha, archive_rel):
            return False
        if not old:
            db.execute('INSERT INTO synthetic_retired_cards VALUES (?,?,?,?,?,?,?)',
                       (relative, sha, archive_rel, event_id,
                        json.dumps(refs, sort_keys=True), 'pending', time.time()))
        elif old[2] in ('restored', 'restore_pending_index'):
            db.execute('UPDATE synthetic_retired_cards SET source_event_id=?,state=? '
                       'WHERE relative_path=? AND sha256=?',
                       (event_id, 'pending', relative, sha))
        elif old[2] not in ('pending', 'queued'):
            return False
    if archive.exists():
        if archive.is_symlink() or _hash(snapshot_beneath(root, archive_rel)) != sha:
            raise ValueError('synthetic_reconcile_archive_conflict')
        if current.exists():
            if _source_snapshot(story, relative) != sha:
                raise ValueError('synthetic_reconcile_card_conflict')
            # The archive already preserves the exact historical bytes from
            # a prior retirement. Remove only the byte-identical live copy.
            current.unlink()
    else:
        if _source_snapshot(story, relative) != sha:
            raise ValueError('synthetic_reconcile_card_changed')
        os.replace(current, archive)
    return True


def _restore_current_cards(root, story, latest):
    """Reactivate an exact generated card only when every source ref is current."""
    with _db(root) as db:
        rows = db.execute('SELECT relative_path,sha256,archive_relative,source_refs,state '
                          'FROM synthetic_retired_cards WHERE state IN (?,?,?)',
                          ('pending', 'queued', 'restore_pending_index')).fetchall()
    restored = 0
    for relative, sha, archive_rel, refs_json, state in rows:
        if latest.get(relative) != sha:
            continue
        refs = json.loads(refs_json)
        if not isinstance(refs, list) or not refs:
            continue
        current_refs = True
        for ref in refs:
            path = ref.get('path') if isinstance(ref, dict) else None
            revision = ref.get('sha256') if isinstance(ref, dict) else None
            if (not isinstance(path, str) or not path.startswith(story.name + '/')
                    or not isinstance(revision, str) or len(revision) != 64
                    or _source_snapshot(story, path[len(story.name) + 1:]) != revision):
                current_refs = False
                break
        if not current_refs:
            # A crash can leave a hidden restore temp before the source moves
            # again. Keep the historical archive and drop only our proven
            # byte-identical temp so it cannot block later scope validation.
            if relative.startswith('.auto-context/wiki/') and '..' not in Path(relative).parts:
                current = story / relative
                temp = current.with_name('.qmd-restore-' + _hash(
                    (relative + sha).encode())[:24] + '.tmp')
                if temp.exists():
                    archive_raw = snapshot_beneath(root, archive_rel)
                    temp_info = temp.lstat()
                    current_info = current.lstat() if current.exists() else None
                    if (_hash(archive_raw) != sha
                            or not stat.S_ISREG(temp_info.st_mode)
                            or temp_info.st_uid != os.getuid()
                            or temp_info.st_nlink not in (1, 2)
                            or (temp_info.st_nlink == 2 and
                                (current_info is None or current_info.st_dev != temp_info.st_dev
                                 or current_info.st_ino != temp_info.st_ino))
                            or _hash(snapshot_beneath(story, temp.relative_to(story))) != sha):
                        raise ValueError('synthetic_reconcile_restore_temp')
                    temp.unlink()
            continue
        current = story / relative
        archive = root / archive_rel
        parts = Path(relative).parts
        if (not relative.startswith('.auto-context/wiki/')
                or any(part in ('', '.', '..') for part in parts)):
            raise ValueError('synthetic_reconcile_restore_scope')
        parent = story
        for part in Path(relative).parts[:-1]:
            parent /= part
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('synthetic_reconcile_restore_parent')
        if archive.exists():
            raw = snapshot_beneath(root, archive_rel)
            if _hash(raw) != sha:
                raise ValueError('synthetic_reconcile_restore_archive')
            temp = current.with_name('.qmd-restore-' + _hash(
                (relative + sha).encode())[:24] + '.tmp')
            if current.exists():
                if _source_snapshot(story, relative) != sha:
                    # A user-edited page at this path needs review. Keep its
                    # bytes and the exact archive; other cards can progress.
                    continue
                if temp.exists():
                    temp_info = temp.lstat()
                    current_info = current.lstat()
                    linked = (temp_info.st_dev == current_info.st_dev
                              and temp_info.st_ino == current_info.st_ino)
                    if (not stat.S_ISREG(temp_info.st_mode)
                            or temp_info.st_uid != os.getuid()
                            or temp_info.st_nlink not in (1, 2)
                            or (temp_info.st_nlink == 2 and not linked)
                            or _hash(snapshot_beneath(story, temp.relative_to(story))) != sha):
                        raise ValueError('synthetic_reconcile_restore_temp')
                    temp.unlink()
            else:
                if not temp.exists():
                    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                 os.O_NOFOLLOW, 0o600)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except BaseException:
                        temp.unlink(missing_ok=True)
                        raise
                if (temp.is_symlink() or temp.stat().st_uid != os.getuid()
                        or temp.stat().st_nlink != 1
                        or _hash(snapshot_beneath(story, temp.relative_to(story))) != sha):
                    raise ValueError('synthetic_reconcile_restore_temp')
                try:
                    os.link(temp, current)
                except FileExistsError:
                    if _source_snapshot(story, relative) != sha:
                        raise ValueError('synthetic_reconcile_restore_conflict')
                temp.unlink()
        elif current.exists() and _source_snapshot(story, relative) == sha:
            # Reservation committed, then the process crashed before move.
            # No archive exists and the original card is still current.
            with _db(root) as db:
                db.execute('DELETE FROM synthetic_retired_cards WHERE '
                           'relative_path=? AND sha256=? AND state=?',
                           (relative, sha, state))
            continue
        else:
            raise ValueError('synthetic_reconcile_restore_missing')
        with _db(root) as db:
            db.execute('UPDATE synthetic_retired_cards SET state=? WHERE '
                       'relative_path=? AND sha256=? AND state IN (?,?,?)',
                       ('restore_pending_index', relative, sha,
                        'pending', 'queued', 'restore_pending_index'))
        restored += 1
    return restored


def _request_index(project):
    """Ask the project's selected Laya index to rescan; no-op without a route."""
    import wiki_selected_laya
    wiki_selected_laya.request_sync(project)


def _process(root, project, story):
    with _db(root) as db:
        latest = {path: sha for path, (_kind, sha) in _ledger(db).items()}
    processed = 0
    for event_id, relative, operation, before, after, state in list(_events(root)):
        if state != 'pending':
            continue
        with _db(root) as db:
            current = db.execute('SELECT sha256 FROM synthetic_source_inventory '
                                 'WHERE relative_path=?', (relative,)).fetchone()
            if current is None:
                raise ValueError('synthetic_reconcile_inventory_missing')
            if before == current[0]:
                # A source can revert before replay (A→B→A). A card pinned to
                # the current A bytes is valid and must not be retired merely
                # because an older transition remains in the journal.
                db.execute('UPDATE synthetic_source_events SET state=?,processed_at=? '
                           'WHERE event_id=? AND state=?',
                           ('superseded', time.time(), event_id, 'pending'))
                continue
        if operation in ('update', 'delete'):
            needs_review = False
            for card, sha, refs in _referencing_cards(story, relative, before):
                if not _retire_card(root, story, event_id, card, sha, refs, latest):
                    needs_review = True
            with _db(root) as db:
                db.execute('UPDATE synthetic_source_events SET state=?,processed_at=? '
                           'WHERE event_id=? AND state=?',
                           ('needs_review' if needs_review else
                            'retired' if operation == 'delete' else 'needs_origin',
                            time.time(), event_id, 'pending'))
            processed += 1
        else:
            with _db(root) as db:
                db.execute('UPDATE synthetic_source_events SET state=?,processed_at=? '
                           'WHERE event_id=? AND state=?',
                           ('needs_origin', time.time(), event_id, 'pending'))
            processed += 1
    # Queue retirement before attempting any restoration. An unrelated
    # user-edited restore target must not strand a stale active index row.
    with _db(root) as db:
        pending = db.execute("SELECT COUNT(*) FROM synthetic_retired_cards WHERE state='pending'").fetchone()[0]
    if pending:
        _request_index(project)
        with _db(root) as db:
            db.execute("UPDATE synthetic_retired_cards SET state='queued' WHERE state='pending'")
    restored = _restore_current_cards(root, story, latest)
    if restored:
        _request_index(project)
        with _db(root) as db:
            db.execute("UPDATE synthetic_retired_cards SET state='restored' "
                       "WHERE state='restore_pending_index'")
    return processed, pending, restored


def _events(root):
    with _db(root) as db:
        return db.execute('SELECT event_id,relative_path,operation,before_sha256,'
                          'after_sha256,state FROM synthetic_source_events '
                          'ORDER BY recorded_at,event_id').fetchall()


def _reconcile_authorized(root, project, story):
    files = _files(story)
    with _db(root) as db:
        detected = _detect(db, story, files)
    processed, queued, restored = _process(root, project, story)
    return {'detected': detected, 'processed': processed,
            'wikiCollectionsQueued': int(queued > 0 or restored > 0),
            'wikiCardsRestored': restored,
            'externalCalls': 0}


def reconcile(cwd):
    import config
    from .synthetic_experiment import shared_for, story_for
    from .synthetic_scope import NOVEL_ROOT, invocation_in_generated_story, policy_for
    found = config.find_project_config(cwd)
    if Path(found['projectRoot']).resolve(strict=False) != NOVEL_ROOT.resolve(strict=False):
        return {'status': 'disabled', 'externalCalls': 0}
    if not invocation_in_generated_story(found, cwd):
        return {'status': 'outside_synthetic_story', 'externalCalls': 0}
    project = Path(found['projectRoot']).resolve(strict=True)
    root = shared_for(project)
    policy_for(root, project, for_inflight_verification=True)
    story = story_for(project).resolve(strict=True)
    return _reconcile_authorized(root, project, story)
