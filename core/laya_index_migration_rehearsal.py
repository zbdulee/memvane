"""Disposable A-int migration and rollback rehearsal, never an operating migrator.

Only a database with this module's synthetic application ID is accepted as the
legacy input. All paths must live in a caller-created marked rehearsal directory.
There is deliberately no QMD parser, hook, installation or runtime selection.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata

from laya_index_a import LayaIndexA
import laya_index_source_port as source_port


LEGACY_APP_ID = 0x4C59534D  # LYSM: never a QMD database
JOURNAL_APP_ID = 0x4C594D4A  # LYMJ
VERSION = 1
MARKER = '.laya-synthetic-rehearsal'


class MigrationBlocked(RuntimeError):
    """A rehearsal prerequisite or rollback fence was not met."""


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':'),
                                     sort_keys=True).encode('utf-8')).hexdigest()


def _exclusive_db(path: Path):
    if path.is_symlink():
        raise MigrationBlocked('path_not_exclusive')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return sqlite3.connect(path)


def create_synthetic_legacy(path: Path, rows, *, policies=None, deletions=(), embeddings=()):
    """Create only a test-format input DB; refusal on an existing path is final."""
    path = Path(path)
    rows = list(rows)
    if policies is None:
        policies = [(scope, scope, 0) for scope in sorted({r[0] for r in rows})]
    with closing(_exclusive_db(path)) as db:
        db.execute('PRAGMA application_id=%d' % LEGACY_APP_ID)
        db.execute('PRAGMA user_version=1')
        db.execute('CREATE TABLE docs(scope TEXT NOT NULL, path TEXT NOT NULL, '
                   'hash TEXT NOT NULL, eligible INTEGER NOT NULL CHECK(eligible IN (0,1)), '
                   'PRIMARY KEY(scope,path))')
        db.execute('CREATE TABLE policies(scope TEXT PRIMARY KEY, worktree TEXT NOT NULL, '
                   'opt_out INTEGER NOT NULL CHECK(opt_out IN (0,1)))')
        db.execute('CREATE TABLE deletions(scope TEXT NOT NULL, path TEXT NOT NULL, '
                   'PRIMARY KEY(scope,path))')
        db.execute('CREATE TABLE embeddings(scope TEXT NOT NULL, path TEXT NOT NULL, '
                   'model TEXT NOT NULL, dimensions INTEGER NOT NULL, '
                   'fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL, '
                   'vector_hex TEXT NOT NULL, '
                   'PRIMARY KEY(scope,path))')
        db.executemany('INSERT INTO docs VALUES(?,?,?,?)', rows)
        db.executemany('INSERT INTO policies VALUES(?,?,?)', policies)
        db.executemany('INSERT INTO deletions VALUES(?,?)', deletions)
        db.executemany('INSERT INTO embeddings VALUES(?,?,?,?,?,?,?)', embeddings)
        db.commit()


def _open_legacy(path: Path):
    if not path.is_file() or path.is_symlink():
        raise MigrationBlocked('legacy_missing_or_symlink')
    db = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        if (db.execute('PRAGMA application_id').fetchone()[0] != LEGACY_APP_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != VERSION
                or db.execute('PRAGMA quick_check').fetchone()[0] != 'ok'):
            raise MigrationBlocked('legacy_schema_or_integrity')
        columns = {'docs': ['scope', 'path', 'hash', 'eligible'],
                   'policies': ['scope', 'worktree', 'opt_out'],
                   'deletions': ['scope', 'path'],
                   'embeddings': ['scope', 'path', 'model', 'dimensions',
                                  'fingerprint', 'content_hash', 'vector_hex']}
        for table, expected in columns.items():
            if [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')] != expected:
                raise MigrationBlocked('legacy_schema_or_integrity')
        return db
    except BaseException:
        db.close()
        raise


def _legacy_snapshot(path: Path):
    with closing(_open_legacy(path)) as db:
        tables = ('docs', 'policies', 'deletions', 'embeddings')
        snapshot = {table: db.execute('SELECT * FROM ' + table).fetchall()
                    for table in tables}
    rows = snapshot['docs']
    if any(not isinstance(scope, str) or not scope or not isinstance(name, str)
           or not name or not isinstance(digest, str) or len(digest) != 64
           or eligible not in (0, 1) for scope, name, digest, eligible in rows):
        raise MigrationBlocked('legacy_row_invalid')
    paths = [(scope, name) for scope, name, _, _ in rows]
    paths.extend(snapshot['deletions'])
    for scope, name in paths:
        if (not isinstance(scope, str) or not scope or not isinstance(name, str)
                or name != unicodedata.normalize('NFC', name)
                or name.startswith('/') or '\\' in name or '\x00' in name
                or any(part in ('', '.', '..') for part in name.split('/'))):
            raise MigrationBlocked('legacy_path_noncanonical')
    for table in tables:
        try:
            snapshot[table].sort()
        except TypeError as exc:
            raise MigrationBlocked('legacy_row_invalid') from exc
    return snapshot


class _NoExclusions:
    """The synthetic legacy schema has no exclusion authority or policy."""

    @staticmethod
    def list_exclusions(_scope):
        return ()


def _snapshot_backup(source: Path, target: Path):
    """SQLite online backup into an owned temp file, then no-clobber publish."""
    if target.exists() or target.is_symlink():
        raise MigrationBlocked('backup_already_exists')
    fd, raw = tempfile.mkstemp(prefix='.laya-backup-', dir=target.parent)
    os.close(fd)
    temp = Path(raw)
    try:
        with closing(_open_legacy(source)) as old, closing(sqlite3.connect(temp)) as new:
            old.backup(new)
            new.commit()
        _legacy_snapshot(temp)
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        os.link(temp, target)  # EEXIST never replaces someone else's backup.
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def _create_journal(path: Path):
    """Publish only a complete single-file journal; never repair an unknown one."""
    fd, raw = tempfile.mkstemp(prefix='.laya-journal-', dir=path.parent)
    os.close(fd)
    temp = Path(raw)
    try:
        with closing(sqlite3.connect(temp, isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('PRAGMA application_id=%d' % JOURNAL_APP_ID)
                db.execute('PRAGMA user_version=1')
                db.execute('CREATE TABLE selection(id INTEGER PRIMARY KEY CHECK(id=1), '
                           'state TEXT NOT NULL, pointer TEXT NOT NULL, '
                           'candidate_digest TEXT, new_write INTEGER NOT NULL, '
                           'plan_digest TEXT, scope TEXT, source_root TEXT, '
                           'source_manifest TEXT, backup_digest TEXT, '
                           'candidate_incarnation TEXT)')
                db.execute("INSERT INTO selection VALUES(1,'old_active','old',NULL,0,"
                           "NULL,NULL,NULL,NULL,NULL,NULL)")
                db.commit()
            except BaseException:
                db.rollback()
                raise
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        os.link(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


class MigrationRehearsal:
    """One explicit synthetic directory; migrate one approved worktree scope."""

    def __init__(self, root):
        self.root = Path(root)
        marker = self.root / MARKER
        if (not self.root.is_dir() or self.root.is_symlink()
                or self.root.resolve() != self.root
                or marker.is_symlink() or not marker.is_file()
                or marker.read_text(encoding='ascii') != 'synthetic-only-v1\n'):
            raise MigrationBlocked('unmarked_rehearsal_root')
        self.old = self.root / 'legacy.sqlite'
        self.backup = self.root / 'backup.sqlite'
        self.restore = self.root / 'restore.sqlite'
        self.candidate = self.root / 'candidate.sqlite'
        self.journal = self.root / 'selection.sqlite'
        if self.journal.is_symlink():
            raise MigrationBlocked('journal_symlink')
        if not self.journal.exists():
            try:
                _create_journal(self.journal)
            except FileExistsError:
                pass  # another creator won; classify its complete file below
        if self.journal.is_symlink() or not self.journal.is_file():
            raise MigrationBlocked('journal_schema_or_integrity')
        self.db = sqlite3.connect(self.journal.resolve(strict=True).as_uri() + '?mode=rw',
                                  uri=True, isolation_level=None, timeout=2)
        columns = ['id', 'state', 'pointer', 'candidate_digest', 'new_write',
                   'plan_digest', 'scope', 'source_root', 'source_manifest',
                   'backup_digest', 'candidate_incarnation']
        if (self.db.execute('PRAGMA application_id').fetchone()[0] != JOURNAL_APP_ID
                or self.db.execute('PRAGMA user_version').fetchone()[0] != VERSION
                or self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok'
                or [r[1] for r in self.db.execute('PRAGMA table_info(selection)')] != columns
                or self.db.execute('SELECT COUNT(*) FROM selection').fetchone()[0] != 1
                or self.db.execute('SELECT id FROM selection').fetchone()[0] != 1):
            self.db.close()
            raise MigrationBlocked('journal_schema_or_integrity')

    def close(self):
        self.db.close()

    def _source(self, source_root):
        path = Path(source_root)
        if path.is_symlink() or path.resolve() != path or self.root not in path.parents:
            raise MigrationBlocked('source_outside_rehearsal')
        return str(path)

    def state(self):
        state, pointer, _, new_write = self.db.execute(
            'SELECT state,pointer,candidate_digest,new_write FROM selection WHERE id=1').fetchone()
        return {'state': state, 'pointer': pointer, 'new_write': bool(new_write),
                'backup_exists': self.backup.exists(), 'candidate_exists': self.candidate.exists(),
                'candidate_unselected': state in ('old_active', 'preparing')
                and self.candidate.exists()}

    def backup_snapshot(self):
        if self.state()['state'] != 'old_active':
            raise MigrationBlocked('selection_not_old')
        _legacy_snapshot(self.old)  # reject non-synthetic DB before creating output
        _snapshot_backup(self.old, self.backup)
        return self._bind_backup()

    def _bind_backup(self):
        backup = _legacy_snapshot(self.backup)
        if _legacy_snapshot(self.old) != backup:
            raise MigrationBlocked('legacy_changed_after_backup')
        digest = _digest(backup)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT backup_digest,state FROM selection WHERE id=1').fetchone()
            if row[0] is not None and row[0] != digest:
                raise MigrationBlocked('backup_mismatch')
            if row[0] is None and row[1] != 'old_active':
                raise MigrationBlocked('backup_unbound')
            self.db.execute('UPDATE selection SET backup_digest=? WHERE id=1', (digest,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return digest

    def restore_proof(self):
        """Restore the backup into a third DB and compare logical rows/scope."""
        original = _legacy_snapshot(self.backup)
        expected = self.db.execute('SELECT backup_digest FROM selection WHERE id=1').fetchone()[0]
        if expected is None or _digest(original) != expected:
            raise MigrationBlocked('restore_mismatch')
        if not self.restore.exists() and not self.restore.is_symlink():
            _snapshot_backup(self.backup, self.restore)
        restored = _legacy_snapshot(self.restore)
        if restored != original:
            raise MigrationBlocked('restore_mismatch')
        return {'rows': len(original['docs']), 'digest': _digest(restored), 'integrity': 'ok'}

    def _candidate_digest(self):
        with closing(LayaIndexA(self.candidate)) as index:
            index.inspect()
            tables = ('generations', 'membership', 'content', 'head', 'receipts',
                      'candidate_seals', 'gen_index', 'exclusions')
            return _digest([(table, sorted(index.db.execute('SELECT * FROM ' + table).fetchall()))
                            for table in tables])

    @staticmethod
    def _selected(snapshot, scope, worktree):
        policy = [p for p in snapshot['policies'] if p[0] == scope]
        if len(policy) != 1 or policy[0][1] != worktree:
            raise MigrationBlocked('worktree_scope_mismatch')
        if policy[0][2]:
            raise MigrationBlocked('opt_out')
        rows = [r for r in snapshot['docs'] if r[0] == scope]
        deleted = {path.casefold() for s, path in snapshot['deletions'] if s == scope}
        if any(path.casefold() in deleted for _, path, _, _ in rows):
            raise MigrationBlocked('deleted_document_resurrection')
        eligible_paths = {path for _, path, _, eligible in rows if eligible}
        return rows, [e for e in snapshot['embeddings']
                      if e[0] == scope and e[1] in eligible_paths]

    def prepare(self, source_root, scope, gen='migrated-1', claim='migration-1', *,
                worktree=None, expected_embedding=None, fault_at=None):
        """Rebuild scoped FTS; vectors are never imported and block cutover."""
        state = self.state()['state']
        if state not in ('old_active', 'preparing', 'candidate_ready',
                         'candidate_pending_embeddings'):
            raise MigrationBlocked('selection_not_old')
        root = self._source(source_root)
        worktree = scope if worktree is None else worktree
        if not self.backup.exists():
            if state != 'old_active':
                raise MigrationBlocked('backup_missing_after_prepare')
            self.backup_snapshot()
        self._bind_backup()
        if fault_at == 'after_backup':
            raise MigrationBlocked('injected_after_backup')
        snapshot = _legacy_snapshot(self.backup)
        if _legacy_snapshot(self.old) != snapshot:
            raise MigrationBlocked('legacy_changed_after_backup')
        old_rows, embeddings = self._selected(snapshot, scope, worktree)
        preflight = source_port.scan(root, scope, _NoExclusions())
        expected = [(scope, f.path, f.sha256, int(f.eligible)) for f in preflight.files]
        if old_rows != expected:
            raise MigrationBlocked('source_or_hash_mismatch')
        plan = _digest([scope, worktree, gen, claim, root, preflight.manifest,
                        _digest(snapshot), expected_embedding])
        if state == 'old_active':
            if self.candidate.exists() or self.candidate.is_symlink():
                raise MigrationBlocked('candidate_already_exists')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                changed = self.db.execute("UPDATE selection SET state='preparing',plan_digest=?,"
                                          "scope=?,source_root=?,source_manifest=? WHERE id=1 "
                                          "AND state='old_active' AND pointer='old'",
                                          (plan, scope, root, preflight.manifest)).rowcount
                if changed != 1:
                    raise MigrationBlocked('selection_changed')
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            if fault_at == 'after_preparing_commit':
                raise MigrationBlocked('injected_after_preparing_commit')
        else:
            saved = self.db.execute('SELECT plan_digest,candidate_digest FROM selection '
                                    'WHERE id=1').fetchone()
            if saved[0] != plan:
                raise MigrationBlocked('migration_conflict')
            if state in ('candidate_ready', 'candidate_pending_embeddings'):
                if self._candidate_digest() != saved[1]:
                    raise MigrationBlocked('candidate_changed')
                return {'state': state, 'result': 'already_prepared',
                        'backup_digest': _digest(snapshot),
                        'candidate_digest': saved[1],
                        'source_manifest': preflight.manifest,
                        'embedding_reuse': 0,
                        'embedding_regeneration_required': len(embeddings)}
        if self.candidate.is_symlink():
            raise MigrationBlocked('candidate_symlink')
        saved_incarnation = self.db.execute(
            'SELECT candidate_incarnation FROM selection WHERE id=1').fetchone()[0]
        if self.candidate.exists() and saved_incarnation is None:
            raise MigrationBlocked('candidate_not_owned')
        if not self.candidate.exists() and saved_incarnation is not None:
            raise MigrationBlocked('candidate_missing_after_prepare')
        with closing(LayaIndexA(self.candidate, create=not self.candidate.exists())) as index:
            incarnation = index.incarnation()
            if saved_incarnation is None:
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    changed = self.db.execute(
                        "UPDATE selection SET candidate_incarnation=? WHERE id=1 "
                        "AND state='preparing' AND plan_digest=? "
                        "AND candidate_incarnation IS NULL",
                        (incarnation, plan)).rowcount
                    if changed != 1:
                        raise MigrationBlocked('candidate_not_owned')
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            elif incarnation != saved_incarnation:
                raise MigrationBlocked('candidate_not_owned')
            if (index.db.execute('SELECT 1 FROM generations WHERE gen<>? OR scope<>? LIMIT 1',
                                 (gen, scope)).fetchone()
                    or index.db.execute('SELECT 1 FROM receipts WHERE claim<>? LIMIT 1',
                                        (claim,)).fetchone()):
                raise MigrationBlocked('candidate_not_owned')
            if fault_at == 'after_candidate_create':
                raise MigrationBlocked('injected_after_candidate_create')
            source = source_port.scan(root, scope, index)
            comparison = [(scope, f.path, f.sha256, int(f.eligible)) for f in source.files]
            if old_rows != comparison or source.manifest != preflight.manifest:
                raise MigrationBlocked('source_or_hash_mismatch')
            claim_digest = _digest([scope, gen, claim, source.manifest])
            source_port.stage_from_source(index, root, scope, gen, base=None,
                                          claim_digest=claim_digest, claim_epoch=0)
            if fault_at == 'after_stage':
                raise MigrationBlocked('injected_after_stage')
            source_port.publish_from_source(index, root, scope, None, gen, claim,
                                            claim_digest=claim_digest, claim_epoch=0)
            if fault_at == 'after_publish':
                raise MigrationBlocked('injected_after_publish')
            index.inspect()
            actual = sorted(index.db.execute('SELECT scope,path,hash,eligible FROM membership '
                                             'WHERE gen=?', (gen,)).fetchall())
            if actual != old_rows:
                raise MigrationBlocked('candidate_membership_mismatch')
        digest = self._candidate_digest()
        # There is no vector adapter/model in this rehearsal. Metadata/content
        # compatibility is recorded, but no embedding bytes are ever reused.
        hashes = {path: hash_ for _, path, hash_, _ in old_rows}
        verified = sum(1 for _, path, model, dim, fingerprint, content_hash, _vector in embeddings
                       if (expected_embedding == (model, dim, fingerprint)
                           and hashes.get(path) == content_hash))
        next_state = 'candidate_pending_embeddings' if embeddings else 'candidate_ready'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed = self.db.execute("UPDATE selection SET state=?,candidate_digest=? "
                                      "WHERE id=1 AND state='preparing' AND pointer='old' "
                                      "AND plan_digest=?", (next_state, digest, plan)).rowcount
            if changed != 1:
                current = self.db.execute('SELECT state,candidate_digest,plan_digest '
                                          'FROM selection WHERE id=1').fetchone()
                if current == (next_state, digest, plan):
                    self.db.rollback()
                    return {'state': next_state, 'result': 'already_prepared',
                            'backup_digest': _digest(snapshot),
                            'candidate_digest': digest,
                            'source_manifest': source.manifest,
                            'embedding_metadata_verified': verified,
                            'embedding_reuse': 0,
                            'embedding_regeneration_required': len(embeddings)}
                raise MigrationBlocked('selection_changed')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {'state': next_state, 'result': 'prepared',
                'backup_digest': _digest(snapshot), 'candidate_digest': digest,
                'source_manifest': source.manifest, 'embedding_metadata_verified': verified,
                'embedding_reuse': 0,
                'embedding_regeneration_required': len(embeddings)}

    def cutover(self, source_root, scope):
        root = self._source(source_root)
        if self.state()['state'] != 'candidate_ready':
            raise MigrationBlocked('candidate_not_ready')
        self.restore_proof()
        if _legacy_snapshot(self.old) != _legacy_snapshot(self.backup):
            raise MigrationBlocked('legacy_changed_after_backup')
        digest = self._candidate_digest()
        with closing(LayaIndexA(self.candidate)) as index:
            expected_incarnation = self.db.execute(
                'SELECT candidate_incarnation FROM selection WHERE id=1').fetchone()[0]
            if index.incarnation() != expected_incarnation:
                raise MigrationBlocked('candidate_not_owned')
            row = index.db.execute('SELECT h.gen,s.source_manifest FROM head h JOIN '
                                   'candidate_seals s ON s.gen=h.gen WHERE h.scope=?',
                                   (scope,)).fetchone()
            if row is None or source_port.scan(root, scope, index).manifest != row[1]:
                raise MigrationBlocked('candidate_source_stale')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed = self.db.execute("UPDATE selection SET state='new_active',pointer='new' "
                                      "WHERE id=1 AND state='candidate_ready' AND pointer='old' "
                                      "AND candidate_digest=? AND source_root=? "
                                      "AND scope=? AND new_write=0",
                                      (digest, root, scope)).rowcount
            if changed != 1:
                raise MigrationBlocked('candidate_or_selection_changed')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return 'new_active'

    def record_new_write(self):
        """Synthetic write fence; real external writers are not intercepted."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed = self.db.execute("UPDATE selection SET state='new_write_seen',new_write=1 "
                                      "WHERE id=1 AND state IN ('new_active','new_write_seen') "
                                      "AND pointer='new'").rowcount
            if changed != 1:
                raise MigrationBlocked('new_not_selected')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return 'new_write_seen'

    def rollback(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT state,pointer,candidate_digest,new_write,'
                                  'scope,source_root,source_manifest '
                                  'FROM selection WHERE id=1').fetchone()
            if row[:2] != ('new_active', 'new') or row[3]:
                raise MigrationBlocked('rollback_blocked_after_write_or_state')
            if self._candidate_digest() != row[2]:
                raise MigrationBlocked('rollback_blocked_candidate_changed')
            backup = _legacy_snapshot(self.backup)
            expected_backup = self.db.execute('SELECT backup_digest FROM selection '
                                              'WHERE id=1').fetchone()[0]
            if _digest(backup) != expected_backup or _legacy_snapshot(self.old) != backup:
                raise MigrationBlocked('rollback_blocked_legacy_changed')
            with closing(LayaIndexA(self.candidate)) as index:
                expected_incarnation = self.db.execute(
                    'SELECT candidate_incarnation FROM selection WHERE id=1').fetchone()[0]
                if index.incarnation() != expected_incarnation:
                    raise MigrationBlocked('rollback_blocked_candidate_changed')
                root = self._source(row[5])
                if source_port.scan(root, row[4], index).manifest != row[6]:
                    raise MigrationBlocked('rollback_blocked_source_changed')
            self.db.execute("UPDATE selection SET state='restored_old',pointer='old' WHERE id=1")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return 'restored_old'


# This second journal rehearses the actual QMD source format. It is deliberately
# separate from the LYSM fixture above and from the installer-owned journal.
QMD_TRANSITION_APP_ID = 0x4c595154  # LYQT; never an operating installer journal
QMD_TRANSITION_VERSION = 1
_QMD_PHASES = frozenset(('legacy', 'candidate_bound', 'synthetic_active',
                         'restoring', 'restored', 'new_write_seen'))


def _qmd_transition_create(path):
    if path.exists() or path.is_symlink():
        raise MigrationBlocked('qmd_transition_already_exists')
    fd, raw = tempfile.mkstemp(prefix='.laya-qmd-transition-', dir=path.parent)
    os.close(fd)
    temp = Path(raw)
    try:
        with closing(sqlite3.connect(temp, isolation_level=None)) as db:
            db.execute(f'PRAGMA application_id={QMD_TRANSITION_APP_ID}')
            db.execute(f'PRAGMA user_version={QMD_TRANSITION_VERSION}')
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE transition(id INTEGER PRIMARY KEY CHECK(id=1), '
                       "origin TEXT NOT NULL CHECK(origin='legacy_qmd'), "
                       'epoch INTEGER NOT NULL, phase TEXT NOT NULL, '
                       'binding TEXT, route_sha TEXT)')
            db.execute("INSERT INTO transition VALUES(1,'legacy_qmd',0,'legacy',NULL,NULL)")
            db.commit()
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        os.link(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


class QmdRouteRehearsal:
    """Disposable tagged-QMD route observations and first-write rollback fence.

    No project route or installer journal is written. Presence/absence is a
    synthetic observation supplied by the fixture, never operating authority.
    """

    def __init__(self, root):
        self.root = Path(root)
        temp = Path(tempfile.gettempdir()).resolve()
        marker = self.root / MARKER
        if (not self.root.is_dir() or self.root.is_symlink()
                or self.root.resolve() != self.root or temp not in self.root.parents
                or marker.is_symlink() or not marker.is_file()
                or marker.read_text(encoding='ascii') != 'synthetic-only-v1\n'):
            raise MigrationBlocked('unmarked_qmd_temp_root')
        self.journal = self.root / 'qmd-transition.sqlite'
        if not self.journal.exists() and not self.journal.is_symlink():
            try:
                _qmd_transition_create(self.journal)
            except FileExistsError:
                pass
        if self.journal.is_symlink() or not self.journal.is_file():
            raise MigrationBlocked('qmd_transition_unavailable')
        self.inspect()

    def _open(self):
        db = sqlite3.connect(self.journal.resolve(strict=True).as_uri() + '?mode=rw',
                             uri=True, timeout=2, isolation_level=None)
        db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def _row(db):
        if (db.execute('PRAGMA application_id').fetchone()[0] != QMD_TRANSITION_APP_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != QMD_TRANSITION_VERSION
                or db.execute('PRAGMA quick_check').fetchone()[0] != 'ok'
                or [r[1] for r in db.execute('PRAGMA table_info(transition)')] !=
                ['id','origin','epoch','phase','binding','route_sha']):
            raise MigrationBlocked('qmd_transition_schema_invalid')
        rows = db.execute('SELECT * FROM transition').fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != 'legacy_qmd':
            raise MigrationBlocked('qmd_transition_schema_invalid')
        _, origin, epoch, phase, raw, route_sha = rows[0]
        if (type(epoch) is not int or epoch < 0 or phase not in _QMD_PHASES
                or ((phase == 'legacy') != (raw is None and route_sha is None))):
            raise MigrationBlocked('qmd_transition_state_invalid')
        try:
            if raw is None:
                binding = None
            else:
                digest, encoded = raw.split(':', 1)
                binding = json.loads(encoded)
                if digest != _digest(binding):
                    raise ValueError('binding_digest_mismatch')
        except (TypeError, ValueError) as exc:
            raise MigrationBlocked('qmd_transition_state_invalid') from exc
        if phase != 'legacy' and (not isinstance(binding, dict)
                                  or not isinstance(route_sha, str)
                                  or len(route_sha) != 64):
            raise MigrationBlocked('qmd_transition_state_invalid')
        return {'origin': origin, 'epoch': epoch, 'phase': phase,
                'binding': binding, 'route_sha': route_sha}

    def inspect(self):
        with closing(self._open()) as db:
            db.execute('BEGIN')
            try:
                row = self._row(db)
                db.commit()
                return row
            except BaseException:
                db.rollback()
                raise

    def _inside(self, value):
        try:
            path = Path(value)
        except (TypeError, ValueError) as exc:
            raise MigrationBlocked('qmd_rehearsal_path_outside_root') from exc
        if (path.is_symlink() or not path.is_file() or path.resolve() != path
                or self.root not in path.parents):
            raise MigrationBlocked('qmd_rehearsal_path_outside_root')
        return path

    @staticmethod
    def _route_hash(route_bytes):
        if type(route_bytes) is not bytes or not route_bytes:
            raise MigrationBlocked('synthetic_route_identity_required')
        return hashlib.sha256(route_bytes).hexdigest()

    @staticmethod
    def _presence(present):
        if type(present) is not bool:
            raise MigrationBlocked('synthetic_route_observation_required')
        return present

    def _cas(self, epoch, phase, allowed, *, check=None, binding=None, route_sha=None):
        with closing(self._open()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = self._row(db)
                if type(epoch) is not int or row['epoch'] != epoch:
                    raise MigrationBlocked('stale_qmd_transition_cas')
                if row['phase'] not in allowed:
                    raise MigrationBlocked('qmd_transition_phase_conflict')
                if check is not None:
                    check(row)
                new_binding = row['binding'] if binding is None else binding
                new_sha = row['route_sha'] if route_sha is None else route_sha
                changed = db.execute('UPDATE transition SET epoch=?,phase=?,binding=?,route_sha=? '
                                     'WHERE id=1 AND epoch=?',
                                     (epoch + 1, phase,
                                      None if new_binding is None else _digest(new_binding) + ':' +
                                      json.dumps(new_binding, sort_keys=True),
                                      new_sha, epoch)).rowcount
                if changed != 1:
                    raise MigrationBlocked('stale_qmd_transition_cas')
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.inspect()

    def _verified(self, row, *, restore=False):
        from laya_qmd_migration import inspect_qmd, verify_restore
        from laya_index_claim_queue import ClaimQueue
        from laya_index_vector import verify_generation_seal
        binding = row['binding']
        source = binding['qmd']
        candidate = binding['candidate']
        queue = binding['queue']
        if inspect_qmd(source['source'], source['format'], source['collection'])['digest'] != source['digest']:
            raise MigrationBlocked('qmd_source_changed')
        for record in (candidate, queue):
            path = self._inside(record['path'])
            info = path.stat()
            if (info.st_dev, info.st_ino) != (record['dev'], record['ino']):
                raise MigrationBlocked('qmd_bound_file_replaced')
        with closing(LayaIndexA(candidate['path'])) as index:
            if index.incarnation() != candidate['incarnation']:
                raise MigrationBlocked('qmd_candidate_evidence_mismatch')
            gen = candidate['generation']
            state = index.db.execute('SELECT scope,state FROM generations WHERE gen=?',
                                     (gen,)).fetchone()
            seal = index.db.execute('SELECT source_manifest,claim_digest FROM candidate_seals '
                                    'WHERE gen=?', (gen,)).fetchone()
            vector = verify_generation_seal(index, gen)
            if (state not in ((candidate['scope'], 'prepared'),
                              (candidate['scope'], 'published'))
                    or seal != (candidate['source_manifest'], source['digest'])
                    or vector['profile_id'] != candidate['vector_profile_id']):
                raise MigrationBlocked('qmd_candidate_evidence_mismatch')
        with closing(sqlite3.connect(Path(queue['path']).as_uri() + '?mode=ro',
                                     uri=True, timeout=2)) as queue_conn:
            queue_conn.execute('BEGIN')
            observed = ClaimQueue._check_database(queue_conn)
            queue_conn.rollback()
        if observed != candidate['incarnation']:
            raise MigrationBlocked('qmd_queue_binding_mismatch')
        if restore:
            proof = verify_restore(source['backup'], source['format'],
                                   source['collection'], source['digest'])
            if proof['restored_digest'] != source['digest']:
                raise MigrationBlocked('qmd_backup_changed')

    def bind(self, epoch, *, source_db, backup_db, source_format, collection,
             expected_digest, candidate_db, queue_db, scope, generation,
             source_manifest, vector_profile_id, route_bytes, candidate_ready):
        """Bind real format/DB seals in a separate synthetic tagged journal."""
        from laya_qmd_migration import FORMATS, inspect_qmd, verify_restore
        from laya_index_claim_queue import ClaimQueue
        from laya_index_vector import verify_generation_seal
        if candidate_ready is not True or source_format not in FORMATS:
            raise MigrationBlocked('qmd_candidate_not_ready')
        route_sha = self._route_hash(route_bytes)
        source, backup, candidate, queue = [self._inside(p) for p in
                                            (source_db, backup_db, candidate_db, queue_db)]
        if len({source, backup, candidate, queue, self.journal}) != 5:
            raise MigrationBlocked('qmd_rehearsal_paths_overlap')
        proof = verify_restore(backup, source_format, collection, expected_digest)
        if (proof['restored_digest'] != expected_digest or
                inspect_qmd(source, source_format, collection)['digest'] != expected_digest):
            raise MigrationBlocked('qmd_source_changed')
        with closing(LayaIndexA(candidate)) as index:
            incarnation = index.incarnation()
            row = index.db.execute('SELECT scope,state FROM generations WHERE gen=?',
                                   (generation,)).fetchone()
            seal = index.db.execute('SELECT source_manifest,claim_digest FROM candidate_seals '
                                    'WHERE gen=?', (generation,)).fetchone()
            vector = verify_generation_seal(index, generation)
            if (row != (scope, 'prepared') or seal != (source_manifest, expected_digest)
                    or vector['profile_id'] != vector_profile_id or vector['scope'] != scope):
                raise MigrationBlocked('qmd_candidate_evidence_mismatch')
        with closing(sqlite3.connect(queue.as_uri() + '?mode=ro', uri=True)) as queue_conn:
            queue_conn.execute('BEGIN')
            binding = ClaimQueue._check_database(queue_conn)
            queue_conn.rollback()
        if binding != incarnation or (queue.stat().st_dev, queue.stat().st_ino) == (
                candidate.stat().st_dev, candidate.stat().st_ino):
            raise MigrationBlocked('qmd_queue_binding_mismatch')
        identity = {'qmd': {'source': str(source), 'backup': str(backup),
                            'format': source_format, 'collection': collection,
                            'digest': expected_digest},
                    'candidate': {'path': str(candidate), 'dev': candidate.stat().st_dev,
                                  'ino': candidate.stat().st_ino, 'incarnation': incarnation,
                                  'scope': scope, 'generation': generation,
                                  'source_manifest': source_manifest,
                                  'vector_profile_id': vector_profile_id},
                    'queue': {'path': str(queue), 'dev': queue.stat().st_dev,
                              'ino': queue.stat().st_ino, 'index_incarnation': binding}}
        return self._cas(epoch, 'candidate_bound', ('legacy',), binding=identity,
                         route_sha=route_sha)

    def activate_observed(self, epoch, route_bytes, present):
        present = self._presence(present)
        digest = self._route_hash(route_bytes)
        def check(row):
            if not present or digest != row['route_sha']:
                raise MigrationBlocked('synthetic_route_not_selected')
            self._verified(row)
        return self._cas(epoch, 'synthetic_active', ('candidate_bound',), check=check)

    def mark_first_new_write(self, epoch):
        return self._cas(epoch, 'new_write_seen', ('synthetic_active',))

    def begin_restore(self, epoch, route_bytes, present):
        present = self._presence(present)
        digest = self._route_hash(route_bytes)
        def check(row):
            if not present or digest != row['route_sha']:
                raise MigrationBlocked('synthetic_route_not_selected')
            self._verified(row)
        return self._cas(epoch, 'restoring', ('synthetic_active',), check=check)

    def finish_restore(self, epoch, present):
        present = self._presence(present)
        def check(row):
            if present:
                raise MigrationBlocked('synthetic_route_still_present')
            self._verified(row, restore=True)
        return self._cas(epoch, 'restored', ('restoring',), check=check)

    def recover(self, present):
        present = self._presence(present)
        row = self.inspect()
        if row['phase'] == 'restoring':
            if present:
                # Keep the fence. The fixture must choose an explicit retry or
                # route removal; never silently reopen the writer.
                return row
            return self.finish_restore(row['epoch'], False)
        if row['phase'] == 'new_write_seen' and not present:
            raise MigrationBlocked('after_write_route_missing_forward_only')
        if row['phase'] == 'synthetic_active' and not present:
            raise MigrationBlocked('active_route_missing_without_restore')
        return row
