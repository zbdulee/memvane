"""Inactive candidate content/provenance storage; no routes or source-body API.

The caller owns the explicit scope and candidate path. Wiki admission validates
stable bounded observations, not an atomic filesystem/Git snapshot. Evidence
records in this file do not grant operational trust or deletion authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

import laya_blob_schema as schema
import laya_blob_values as v
from laya_blob_values import BlobStoreError
import laya_checkout_view as checkout
import laya_view_fts as view_fts

NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC

# Legacy frontmatter trust: the v6 reader's card rule, carried as an unbound
# attestation. Migration import and the normal writer ingest share it, so the
# same bytes, worktree, branch and scan give the same trust digest. The rule
# reads only card bytes; no current HEAD or raw file is claimed as its proof.
LEGACY_ENGINE_IDENTITY = 'memvane:v6-reader'
LEGACY_RESULT_SCHEMA = 'memvane-legacy-card-evidence-v1'


def legacy_card_evidence(row, decision):
    """Describe the existing reader's trust evidence, without granting authority."""
    import laya_host_response
    import source_spans
    import wiki_card

    locators = laya_host_response._verified_wiki_locations(
        row, decision, frozenset(), tuple(decision['recallSettings']['skipPaths']),
        current_source=False)
    if locators is None:
        return None
    body = row['body'].replace('\r\n', '\n').replace('\r', '\n')
    block = body[4:body.index('\n---\n', 4)]
    revisions = wiki_card.frontmatter_source_revisions(block)
    _, spans = source_spans.read_card_spans(block)
    return {'revisions': revisions, 'spans': spans, 'locators': list(locators)}


def legacy_scan(provenance_scan):
    v.exact(provenance_scan, ('id', 'observed_ns'))
    v.identifier(provenance_scan['id'], v.UUID)
    v.integer(provenance_scan['observed_ns'])
    return provenance_scan


def _database_path(value):
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or '\x00' in raw:
            v.fail('invalid_explicit_path')
        return Path(raw).absolute()
    except (TypeError, ValueError, OSError):
        v.fail('invalid_explicit_path')


def _node(info):
    return {'dev': info.st_dev, 'ino': info.st_ino}


def _file_node(info):
    return {**_node(info), 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'ctime_ns': info.st_ctime_ns}


def _dirfd(path, *, private=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        v.fail('invalid_explicit_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            v.fail('private_directory_required')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _db_file(path):
    fd = _dirfd(path.parent, private=True)
    try:
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            v.fail('private_database_required')
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                side = os.stat(path.name + suffix, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (suffix != '-journal' or not stat.S_ISREG(side.st_mode)
                    or side.st_uid != os.getuid() or side.st_nlink != 1
                    or stat.S_IMODE(side.st_mode) != 0o600):
                v.fail('unsafe_database_sidecar')
        return _node(info)
    finally:
        os.close(fd)


def _regular(root, relative, limit):
    """Read from a fixed descriptor chain, then re-resolve its path identity."""
    rootfd = _dirfd(root)
    parent = os.dup(rootfd)
    leaf = None
    try:
        parts = relative.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=parent)
        before = os.fstat(leaf)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_size > limit):
            v.fail('source_file_invalid')
        raw = bytearray()
        while True:
            part = os.read(leaf, min(65536, limit + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
            if len(raw) > limit:
                v.fail('source_file_too_large')
        if _file_node(os.fstat(leaf)) != _file_node(before):
            v.fail('source_file_changed')
        current = os.dup(rootfd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=current)
                os.close(current); current = child
            now = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            if not stat.S_ISREG(now.st_mode) or _file_node(now) != _file_node(before):
                v.fail('source_file_changed')
        finally:
            os.close(current)
        return bytes(raw), _file_node(before), _node(os.fstat(rootfd))
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(parent); os.close(rootfd)


def _current_file_node(root, relative):
    fd = _dirfd(root)
    try:
        parts = relative.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > v.MAX_BODY):
            v.fail('source_file_invalid')
        return _file_node(info)
    finally:
        os.close(fd)


SETTINGS_LIMIT = 1024 * 1024


def _settings_identity(root):
    """The settings identity a worktree binding commits to, read once.

    It is the v6 route's settings identity (``settings_identity_digest``:
    ``{name, role, sourceRoot}`` per derived collection, plus the project
    root): the absolute rows are the binding's ``collections``, and the store
    policy digests the same rows checkout-relative (folder spelling
    normalized), so linked worktrees share it. Settings bytes and fields
    outside that identity (``skipPaths`` and other options) are not bound;
    readers take ``skipPaths`` from ``config`` here at each use.
    """
    import config
    import laya_index_host_route as routes
    import project_dir
    from pathlib import PurePosixPath
    settings = project_dir.settings_path(root)
    if settings is None:
        v.fail('wiki_settings_missing')
    raw, _, _ = _regular(settings.parent, settings.name, SETTINGS_LIMIT)
    value = json.loads(raw.decode('utf8'), object_pairs_hook=routes._pairs)
    if not isinstance(value, dict):
        v.fail('wiki_binding_invalid')
    collections = routes._derived_collections({'schema': routes.SCHEMA_V2}, root, raw)
    routes.selected_collection_roots(root, collections)
    wiki = Path(next(iter(routes.wiki_index_roots(collections).values())))
    rows = []
    for item in collections:
        parts = PurePosixPath(os.path.relpath(item['sourceRoot'], root).replace(os.sep, '/')).parts
        if parts and parts[0] in project_dir.NAMES:
            parts = (project_dir.CURRENT, *parts[1:])
        rows.append({'name': item['name'], 'role': item['role'],
                     'sourceRoot': str(PurePosixPath(*parts)) if parts else '.'})
    policy = v.digest('memvane-candidate-policy-v2', {
        'schema': routes.SETTINGS_IDENTITY_SCHEMA,
        'collections': sorted(rows, key=lambda item: item['name'])})
    config_value = config.normalize_config(config.expand_minimal_settings(value, project_dir.CURRENT),
                                           project_dir.CURRENT)
    return {'policy_digest': policy, 'collections': collections, 'wiki_root': str(wiki),
            'raw_config': value, 'config': config_value}


def _bound_settings(saved):
    """Current settings identity of a saved worktree row; any drift is a binding change."""
    try:
        current = _settings_identity(Path(saved['project_root']))
        binding = v.parse_canonical(saved['binding_json'])
        if (current['policy_digest'] != saved['policy_digest'] or current['wiki_root'] != saved['wiki_root']
                or not isinstance(binding, dict) or current['collections'] != binding.get('collections')):
            v.fail('wiki_binding_changed')
        return current
    except BlobStoreError:
        raise
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        v.fail('wiki_settings_invalid')


def _observation(project_root, repository, worktree, *, deadline=None):
    try:
        limit = None if deadline is None else min(deadline, time.monotonic()+checkout.CAPTURE_SECONDS)
        capture = checkout.capture_view(str(project_root), repository, worktree, deadline=limit)
        root = Path(capture['projectRoot'])
        identity = _settings_identity(root)
        fd = _dirfd(Path(identity['wiki_root']))
        try:
            node = _node(os.fstat(fd))
        finally:
            os.close(fd)
        limit = None if deadline is None else min(deadline, time.monotonic()+checkout.CAPTURE_SECONDS)
        if not checkout.capture_matches(capture, repository, worktree, deadline=limit,
                                        _raise_timeout=deadline is not None):
            v.fail('checkout_changed')
        again = _settings_identity(root)
        if any(again[k] != identity[k] for k in ('policy_digest', 'collections', 'wiki_root')):
            v.fail('wiki_binding_changed')
        return {'project_root': str(root), 'policy_digest': identity['policy_digest'],
                'wiki_root': identity['wiki_root'], 'wiki_dev': node['dev'], 'wiki_ino': node['ino'],
                'collections': identity['collections'], 'capture': capture}
    except BlobStoreError:
        raise
    except checkout.CheckoutUnavailable as exc:
        if deadline is not None and exc.reason == 'checkout_budget_exhausted' and time.monotonic() >= deadline:
            raise
        v.fail('wiki_binding_invalid')
    except (OSError, ValueError, KeyError, StopIteration):
        v.fail('wiki_binding_invalid')


# Binding evidence: the v6 settings identity (project root, collections and
# the policy over them), the wiki root node and the checkout record.
BINDING_SCHEMA = 'memvane-blob-worktree-v2'
BINDING_COLUMNS = ('policy_digest', 'wiki_root', 'wiki_dev', 'wiki_ino')
BINDING_FIELDS = ('project_root', *BINDING_COLUMNS, 'collections', 'record')


def _worktree_row(project_root, repository, worktree, *, deadline=None):
    observation = _observation(project_root, repository, worktree, deadline=deadline)
    evidence = {k: val for k, val in observation.items() if k != 'capture'}
    evidence['record'] = worktree
    row = {'worktree_id': worktree['worktreeId'],
           'canonical_root': worktree['checkout']['path'],
           'git_admin_dir': worktree['gitDir']['path'] if worktree['gitDir'] else None,
           'root_dev': worktree['checkout']['dev'], 'root_ino': worktree['checkout']['ino'],
           'admin_dev': worktree['gitDir']['dev'] if worktree['gitDir'] else None,
           'admin_ino': worktree['gitDir']['ino'] if worktree['gitDir'] else None,
           'binding_digest': v.digest(BINDING_SCHEMA, evidence),
           'binding_json': v.canonical(evidence),
           'project_root': observation['project_root'], 'record_json': v.canonical(worktree),
           **{k: observation[k] for k in BINDING_COLUMNS}}
    return row, observation


def _insert(db, table, row):
    columns = tuple(row)
    db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
               tuple(row[k] for k in columns))


def _header(db, repository, scope):
    if (db.execute('PRAGMA application_id').fetchone()[0] != schema.APP_ID
            or db.execute('PRAGMA user_version').fetchone()[0] != schema.SCHEMA_VERSION):
        v.fail('candidate_format_mismatch')
    if schema.manifest(db) != schema.expected_manifest():
        v.fail('candidate_schema_mismatch')
    row = db.execute('SELECT * FROM store_meta WHERE singleton=1').fetchone()
    if (row is None or row['format'] != schema.FORMAT
            or row['schema_version'] != schema.SCHEMA_VERSION
            or row['repository_json'] != v.canonical(repository)
            or row['scope_id'] != scope or row['schema_digest'] != schema.expected_manifest()):
        v.fail('candidate_binding_mismatch')
    v.identifier(row['incarnation'], v.UUID)
    v.identifier(row['settings_policy_digest'])
    return dict(row)


def _worktree_record(row, repository, policy_digest):
    record = v.parse_canonical(row['record_json'])
    binding = v.parse_canonical(row['binding_json'])
    v.exact(binding, BINDING_FIELDS)
    if (not isinstance(record, dict) or set(record) != checkout.WORKTREE_FIELDS
            or record.get('schema') != checkout.WORKTREE_SCHEMA
            or record.get('worktreeId') != row['worktree_id']
            or record.get('repoId') != repository['repoId']
            or record.get('repositoryDigest') != checkout._digest(repository)
            or not all(checkout._identity(record.get(k)) for k in ('project', 'checkout'))
            or (record.get('gitDir') is not None and not checkout._identity(record['gitDir']))
            or binding.get('record') != record
            or v.digest(BINDING_SCHEMA, binding) != row['binding_digest']):
        v.fail('worktree_evidence_invalid')
    expected = dict(project_root=record['project']['path'],
                    canonical_root=record['checkout']['path'], root_dev=record['checkout']['dev'],
                    root_ino=record['checkout']['ino'],
                    git_admin_dir=record['gitDir']['path'] if record['gitDir'] else None,
                    admin_dev=record['gitDir']['dev'] if record['gitDir'] else None,
                    admin_ino=record['gitDir']['ino'] if record['gitDir'] else None)
    if (any(row[k] != val for k, val in expected.items())
                or row['policy_digest'] != policy_digest
                or any(binding.get(k) != row[k] for k in ('project_root', *BINDING_COLUMNS))):
        v.fail('worktree_evidence_invalid')
    return record



class _VectorPage:
    """The open transaction and caches of one ``BlobStore._vector_page``."""

    def __init__(self, store, db, write):
        self.store, self.db, self.write = store, db, write
        self.cache = {'worktrees': {}, 'captures': set()}
        self.memo = {}

    @contextmanager
    def member(self):
        """One member's work: kept on success, rolled back alone on failure."""
        self.db.execute('SAVEPOINT vector_member')
        try:
            yield
        except BaseException:
            self.db.execute('ROLLBACK TO vector_member')
            self.db.execute('RELEASE vector_member')
            # Memos may name rows the rollback removed.
            self.memo.clear()
            raise
        self.db.execute('RELEASE vector_member')

class BlobStore:
    @classmethod
    def create(cls, path, *, project_root, repository, worktree, scope_id):
        repository = v.repository(repository); v.identifier(scope_id, v.UUID)
        path = _database_path(path)
        temp = path.parent / ('.' + path.name + '.create-' + uuid.uuid4().hex)
        fd = None
        try:
            fd = _dirfd(path.parent, private=True)
            for name in (path.name, *(path.name + s for s in ('-wal', '-shm', '-journal'))):
                try:
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                v.fail('candidate_already_exists')
            row, observation = _worktree_row(project_root, repository, worktree)
            created = time.time_ns()
            tmpfd = os.open(temp.name, os.O_CREAT | os.O_EXCL | os.O_RDWR | NOFOLLOW,
                            0o600, dir_fd=fd)
            os.close(tmpfd)
            db = sqlite3.connect(temp.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=2, cached_statements=0)
            try:
                view_fts.ConnectionGuard(db)
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                schema.create(db)
                common = repository['commonGitDir'] or {}
                _insert(db, 'store_meta', dict(singleton=1, format=schema.FORMAT, schema_version=schema.SCHEMA_VERSION,
                    repo_id=repository['repoId'], scope_id=scope_id, incarnation=uuid.uuid4().hex,
                    common_git_dir=common.get('path'), common_dev=common.get('dev'), common_ino=common.get('ino'),
                    project_rel=repository['projectRelative'], git_object_format=repository['gitObjectFormat'],
                    repository_json=v.canonical(repository), settings_policy_digest=row['policy_digest'],
                    schema_digest=schema.expected_manifest(), created_ns=created))
                _insert(db, 'worktrees', {**row, 'registered_ns': created})
                if _worktree_row(project_root, repository, worktree)[0] != row:
                    v.fail('wiki_binding_changed')
                db.commit()
            finally:
                db.close()
            filefd = os.open(temp.name, os.O_RDONLY | NOFOLLOW, dir_fd=fd)
            try:
                os.fsync(filefd)
            finally:
                os.close(filefd)
            os.link(temp.name, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            os.unlink(temp.name, dir_fd=fd)
            os.fsync(fd)
        except BlobStoreError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            v.fail('candidate_create_failed')
        finally:
            if fd is not None:
                for name in (temp.name, temp.name + '-journal'):
                    try:
                        os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                os.close(fd)
        return cls.open(path, repository=repository, scope_id=scope_id)

    @classmethod
    def open(cls, path, *, repository, scope_id):
        return cls._open(path, repository=repository, scope_id=scope_id, reader_only=False)

    @classmethod
    def _open(cls, path, *, repository, scope_id, reader_only, deadline=None):
        repository = v.repository(repository); v.identifier(scope_id, v.UUID)
        path = _database_path(path)
        if reader_only and deadline is not None:
            try:
                deadline = checkout._deadline(deadline)
            except checkout.CheckoutUnavailable as exc:
                v.fail('fts_deadline_exceeded' if exc.reason == 'checkout_budget_exhausted'
                       else 'fts_query_invalid')
        db = None
        try:
            identity = _db_file(path)
            remaining = 2 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                v.fail('fts_deadline_exceeded')
            ro = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=min(2,remaining), isolation_level=None, cached_statements=0)
            try:
                if deadline is not None:
                    ro.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                ro.execute('PRAGMA query_only=ON')
                ro.row_factory = sqlite3.Row
                ro.execute('BEGIN')
                initial_meta = _header(ro, repository, scope_id)
                if reader_only:
                    ro.rollback()
                    db, ro = ro, None
            finally:
                if ro is not None:
                    ro.close()
            if not reader_only:
                db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2, isolation_level=None, cached_statements=0)
            fts_guard = view_fts.ConnectionGuard(db)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA recursive_triggers=ON')
            if not reader_only:
                db.execute('PRAGMA synchronous=FULL')
            if _db_file(path) != identity:
                v.fail('candidate_file_changed')
            final_meta = _header(db, repository, scope_id)
            if final_meta != initial_meta:
                v.fail('candidate_binding_changed')
            if reader_only and deadline is not None:
                db.set_progress_handler(None, 0)
                if time.monotonic() >= deadline:
                    v.fail('fts_deadline_exceeded')
            obj = object.__new__(cls)
            obj._path, obj._identity, obj._repository, obj._scope = path, identity, repository, scope_id
            obj._db = db
            obj._reader_only = reader_only
            obj._fts_guard = fts_guard
            obj._incarnation, obj._policy = initial_meta['incarnation'], initial_meta['settings_policy_digest']
            return obj
        except BlobStoreError:
            if db is not None: db.close()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            if db is not None: db.close()
            if reader_only and deadline is not None:
                from laya_view_reader import _sqlite_interrupted
                if _sqlite_interrupted(exc) and time.monotonic() >= deadline:
                    v.fail('fts_deadline_exceeded')
            v.fail('candidate_open_failed')

    def _require_write(self):
        if self._reader_only:
            v.fail('reader_write_forbidden')

    @contextmanager
    def _vector_page(self, *, write):
        """One store session for a page of vector-copy work (shadow migration).

        The page's single transaction checks the store header once; every
        store call made inside it joins that transaction instead of opening
        its own. Within the page a blob's admission proof, a recipe, a profile,
        a chunk set and a vector input list are each proved once and reused:
        they cannot change inside one transaction except through the page's
        own writes, which never replace an existing row. ``member()`` wraps
        one member's work in a savepoint, so a member that fails (or runs out
        of time) leaves nothing behind while earlier members stay; on leaving
        the page the transaction commits what the members completed.
        """
        if write:
            self._require_write()
        if getattr(self, '_page', None) is not None:
            v.fail('vector_page_nested')
        with self._session(write=write) as db:
            page = _VectorPage(self, db, write)
            self._page = page
            try:
                yield page
            finally:
                self._page = None

    def _page_memo(self, kind, key, compute):
        page = getattr(self, '_page', None)
        if page is None:
            return compute()
        memo = page.memo.setdefault(kind, {})
        if key not in memo:
            memo[key] = compute()
        return memo[key]

    @contextmanager
    def _session(self, *, write=False):
        page = getattr(self, '_page', None)
        if page is not None:
            # A store call inside a vector page joins its transaction.
            if write and not page.write:
                v.fail('reader_write_forbidden')
            yield self._db
            return
        if write:
            self._require_write()
        try:
            if _db_file(self._path) != self._identity:
                v.fail('candidate_file_changed')
            self._db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            meta = _header(self._db, self._repository, self._scope)
            if meta['incarnation'] != self._incarnation or meta['settings_policy_digest'] != self._policy:
                v.fail('candidate_binding_changed')
            yield self._db
            self._db.commit()
        except BaseException as exc:
            self._db.rollback()
            if isinstance(exc, (sqlite3.Error, OSError)):
                sqlite_code = getattr(exc, 'sqlite_errorcode', None)
                if ((isinstance(exc, OSError) and exc.errno == errno.ENOSPC)
                        or (type(sqlite_code) is int and sqlite_code & 0xff == getattr(sqlite3, 'SQLITE_FULL', 13))
                        or (isinstance(exc, sqlite3.OperationalError)
                            and str(exc) == 'database or disk is full')):
                    v.fail('candidate_storage_full')
                v.fail('candidate_busy' if 'locked' in str(exc) or 'busy' in str(exc) else 'candidate_io_failed')
            raise

    def register_worktree(self, project_root, worktree):
        self._require_write()
        row, _ = _worktree_row(project_root, self._repository, worktree)
        with self._session(write=True) as db:
            if row['policy_digest'] != self._policy:
                v.fail('worktree_policy_mismatch')
            old = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (row['worktree_id'],)).fetchone()
            if old:
                if any(old[k] != val for k, val in row.items()):
                    v.fail('worktree_binding_conflict')
            else:
                _insert(db, 'worktrees', {**row, 'registered_ns': time.time_ns()})
        return row['worktree_id']

    def _page_blob(self, blob_sha):
        """``_blob`` once per blob inside a vector page, with its admission caches."""
        page = getattr(self, '_page', None)
        if page is None:
            return self._blob(blob_sha)
        return self._page_memo('blob', blob_sha, lambda: self._blob(blob_sha, cache=page.cache))

    def _blob(self, blob_sha, *, cache=None):
        v.identifier(blob_sha)
        row = self._db.execute('SELECT w.blob_sha,w.byte_length,substr(CAST(w.body AS BLOB),1,?) AS body_bytes,'
                               'a.worktree_id,a.proof_digest,a.proof_json FROM wiki_blobs w '
                               'LEFT JOIN blob_admissions a ON a.blob_sha=w.blob_sha WHERE w.blob_sha=?',
                               (v.MAX_BODY + 1, blob_sha)).fetchone()
        if row is None:
            v.fail('blob_missing')
        v.integer(row['byte_length'], 0, v.MAX_BODY)
        raw = row['body_bytes']
        try:
            body = raw.decode('utf8')
        except (AttributeError, UnicodeError):
            v.fail('blob_invalid')
        if len(raw) != row['byte_length'] or v.hash_bytes(raw) != blob_sha or len(raw) > v.MAX_BODY:
            v.fail('blob_conflict')
        if row['proof_json'] is None:
            v.fail('blob_admission_missing')
        admission = row
        proof = v.parse_canonical(admission['proof_json'])
        worktree_id = admission['worktree_id']
        if cache is not None and worktree_id in cache['worktrees']:
            saved, worktree = cache['worktrees'][worktree_id]
        else:
            saved = self._db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
            if saved is None:
                v.fail('blob_admission_invalid')
            worktree = _worktree_record(saved, self._repository, self._policy)
            if cache is not None:
                cache['worktrees'][worktree_id] = (saved, worktree)
        legacy = isinstance(proof, dict) and proof.get('schema') == 'memvane-v6-wiki-admission-v1'
        if legacy:
            v.exact(proof, ('schema', 'blob_sha', 'worktree_id', 'path', 'binding_digest',
                            'wiki_root', 'wiki_identity', 'repository_digest',
                            'capture', 'endpoint_id', 'observation'))
        else:
            v.exact(proof, ('blob_sha', 'worktree_id', 'path', 'file', 'binding_digest',
                            'wiki_root', 'wiki_identity', 'repository_digest', 'capture'))
        capture = proof['capture']
        capture_key = (worktree_id, v.canonical(capture)) if cache is not None else None
        capture_valid = cache is not None and capture_key in cache['captures']
        if not capture_valid:
            capture_valid = (checkout._capture_shape(capture)
                and capture['repoId'] == self._repository['repoId']
                and capture['worktreeId'] == saved['worktree_id']
                and capture['checkoutDigest'] == checkout._digest({k: val for k, val in capture.items() if k != 'checkoutDigest'})
                and all(capture['evidence'][k] == worktree[k] for k in ('project', 'checkout', 'gitDir')))
            if capture_valid and cache is not None:
                cache['captures'].add(capture_key)
        if (proof.get('blob_sha') != blob_sha or proof['worktree_id'] != saved['worktree_id']
                or proof['binding_digest'] != saved['binding_digest']
                or proof['wiki_root'] != saved['wiki_root']
                or proof['wiki_identity'] != {'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']}
                or proof['repository_digest'] != worktree['repositoryDigest']
                or not capture_valid
                or v.digest('memvane-v6-wiki-admission-v1' if legacy else 'memvane-wiki-admission-v1', proof)
                   != admission['proof_digest']):
            v.fail('blob_admission_invalid')
        v.relative(proof['path'])
        if legacy:
            v.identifier(proof['endpoint_id'])
            v.identifier(proof['observation'])
            return body
        v.exact(proof['file'], ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'))
        for number in proof['file'].values():
            v.integer(number)
        if proof['file']['size'] != row['byte_length']:
            v.fail('blob_admission_invalid')
        return body

    def ingest_card(self, worktree_id, path):
        return self._ingest(worktree_id, path)[0]

    def ingest_attested_card(self, worktree_id, path, *, decision, provenance_scan):
        """Admit a card plus, when its frontmatter passes the reader rule, its unbound attestation.

        ``decision`` is the caller's selected-route decision for this store's
        project and wiki root; a card that fails the rule is admitted untrusted.
        """
        legacy_scan(provenance_scan)
        sha, trust = self._ingest(worktree_id, path, attest=(decision, provenance_scan))
        return {'blob_sha': sha, 'trust_digest': trust}

    def _put_legacy_attestation(self, db, *, blob_sha, worktree_id, branch_key, evidence,
                                provenance_scan, verify_only=False):
        sources, seen_fingerprints, assigned_spans = [], set(), set()
        for revision in evidence['revisions']:
            fingerprint = self._put_fingerprint(db, dict(
                worktree_id=worktree_id, branch_key=branch_key,
                path=revision['path'], source_sha=revision['sha256'],
                byte_length=revision['size'], head_oid=None, tracked_state='unknown',
                binding_state='legacy_unbound', scan_id=provenance_scan['id'],
                observed_ns=provenance_scan['observed_ns']), verify_only=verify_only)
            if fingerprint not in seen_fingerprints:
                pair = (revision['path'], revision['sha256'])
                spans = ([{'kind': 'legacy-lines-v1', **span} for span in evidence['spans']
                          if (span['path'], span['sourceSha256']) == pair]
                         if pair not in assigned_spans else [])
                sources.append({'fingerprint_id': fingerprint, 'spans': spans})
                seen_fingerprints.add(fingerprint)
                assigned_spans.add(pair)
        # Card bytes and the rule's output only: the attestation is keyed by
        # blob, and the reader endpoint is already recorded by the admission.
        receipt = v.digest(LEGACY_RESULT_SCHEMA, {'hash': blob_sha, 'evidence': evidence})
        return self._put_attestation(db, dict(blob_sha=blob_sha,
            verification_kind='legacy_verified', engine_identity=LEGACY_ENGINE_IDENTITY,
            policy_digest=self._policy, receipt=dict(schema='memvane-card-evidence-v1',
                verdict='verified', result_digest=receipt, prior_trust=None), sources=sources),
            verify_only=verify_only)

    def _ingest(self, worktree_id, path, *, attest=None):
        self._require_write()
        v.identifier(worktree_id, v.UUID); v.relative(path)
        if not path.endswith('.md') or (len(path.split('/')) == 1 and path in ('SCHEMA.md', 'index.md', 'log.md')):
            v.fail('wiki_card_path_invalid')
        with self._session() as db:
            saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
            if saved is None:
                v.fail('worktree_missing')
            saved = dict(saved)
        if attest is not None:
            decision = attest[0]
            collections = decision.get('collections') if type(decision) is dict else None
            if (type(decision) is not dict or decision.get('projectRoot') != saved['project_root']
                    or decision.get('scopeId') != self._scope or type(collections) is not list
                    or not any(type(item) is dict and item.get('role') == 'wiki'
                               and type(item.get('sourceRoot')) is str
                               and str(Path(item['sourceRoot'])) == saved['wiki_root']
                               for item in collections)):
                v.fail('attestation_decision_mismatch')
        worktree = v.parse_canonical(saved['record_json'])
        row, before = _worktree_row(saved['project_root'], self._repository, worktree)
        if any(saved[k] != val for k, val in row.items()):
            v.fail('wiki_binding_changed')
        try:
            raw, node, rootnode = _regular(saved['wiki_root'], path, v.MAX_BODY)
            body = raw.decode('utf8')
            if body.encode('utf8') != raw:
                v.fail('wiki_utf8_invalid')
            sha = v.hash_bytes(raw)
            after, observation = _worktree_row(saved['project_root'], self._repository, worktree)
            if (after != row or rootnode != {'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']}
                    or not checkout.capture_matches(before['capture'], self._repository, worktree)):
                v.fail('wiki_admission_changed')
            proof = dict(blob_sha=sha, worktree_id=worktree_id, path=path, file=node,
                         binding_digest=row['binding_digest'],
                         wiki_root=saved['wiki_root'], wiki_identity=rootnode,
                         repository_digest=worktree['repositoryDigest'], capture=observation['capture'])
            evidence = None
            if attest is not None:
                try:
                    evidence = legacy_card_evidence({'path': path, 'body': body}, attest[0])
                except (KeyError, TypeError, AttributeError):
                    v.fail('attestation_decision_mismatch')
            trust = None
            with self._session(write=True) as db:
                old = db.execute('SELECT blob_sha FROM wiki_blobs WHERE blob_sha=?', (sha,)).fetchone()
                if old:
                    if self._blob(sha) != body:
                        v.fail('blob_conflict')
                else:
                    now = time.time_ns()
                    _insert(db, 'wiki_blobs', dict(blob_sha=sha, body=body, byte_length=len(raw), created_ns=now))
                    _insert(db, 'blob_admissions', dict(blob_sha=sha, worktree_id=worktree_id,
                            proof_digest=v.digest('memvane-wiki-admission-v1', proof),
                            proof_json=v.canonical(proof), created_ns=now))
                if evidence is not None:
                    trust = self._put_legacy_attestation(db, blob_sha=sha, worktree_id=worktree_id,
                        branch_key=observation['capture']['branchKey'], evidence=evidence,
                        provenance_scan=attest[1])
                if _current_file_node(saved['wiki_root'], path) != node:
                    v.fail('wiki_card_changed')
                _bound_settings(saved)
            return sha, trust
        except BlobStoreError:
            raise
        except (OSError, UnicodeError, checkout.CheckoutUnavailable):
            v.fail('wiki_card_unavailable')

    def document(self, blob_sha):
        with self._session():
            body = self._page_blob(blob_sha)
            path = f'blobs/{blob_sha}.md'
            return {'body': body, 'path': path, 'title': v.markdown_title(body, blob_sha)}

    def register_recipe(self, spec):
        self._require_write()
        spec = v.recipe(spec)
        key, canonical = v.digest('memvane-chunk-recipe-v1', spec), v.canonical(spec)
        with self._session(write=True) as db:
            old = db.execute('SELECT canonical_spec FROM chunk_recipes WHERE recipe_id=?', (key,)).fetchone()
            if old:
                if self._recipe(key) != spec:
                    v.fail('recipe_conflict')
            else:
                _insert(db, 'chunk_recipes', dict(recipe_id=key, canonical_spec=canonical))
        return key

    def _recipe(self, key):
        return self._page_memo('recipe', key, lambda: self._recipe_row(key))

    def _recipe_row(self, key):
        v.identifier(key)
        row = self._db.execute('SELECT canonical_spec FROM chunk_recipes WHERE recipe_id=?', (key,)).fetchone()
        if row is None:
            v.fail('recipe_missing')
        spec = v.recipe(v.parse_canonical(row[0]))
        if v.digest('memvane-chunk-recipe-v1', spec) != key:
            v.fail('recipe_invalid')
        return spec

    @staticmethod
    def _profile_fields(spec):
        return {**{k: spec[k] for k in ('model_id', 'model_sha', 'embedding_fingerprint', 'dimension', 'metric')},
                'input_recipe': v.canonical({k: spec[k] for k in
                    ('document_input_recipe', 'query_input_recipe', 'query_template')}),
                'canonical_spec': v.canonical(spec)}

    def register_profile(self, spec):
        self._require_write()
        spec = v.profile(spec)
        key = v.digest('memvane-vector-profile-v1', spec)
        with self._session(write=True) as db:
            old = db.execute('SELECT profile_id FROM vector_profiles WHERE profile_id=?', (key,)).fetchone()
            if old:
                if self._profile(key) != spec:
                    v.fail('profile_conflict')
            else:
                _insert(db, 'vector_profiles', dict(profile_id=key, **self._profile_fields(spec)))
        return key

    def _profile(self, key):
        return self._page_memo('profile', key, lambda: self._profile_row(key))

    def _profile_row(self, key):
        v.identifier(key)
        row = self._db.execute('SELECT * FROM vector_profiles WHERE profile_id=?', (key,)).fetchone()
        if row is None:
            v.fail('profile_missing')
        spec = v.profile(v.parse_canonical(row['canonical_spec']))
        if (v.digest('memvane-vector-profile-v1', spec) != key
                or any(row[k] != val for k, val in self._profile_fields(spec).items())):
            v.fail('profile_invalid')
        return spec

    def _chunks(self, blob, recipe_id):
        return self._page_memo('chunks', (blob, recipe_id), lambda: self._chunk_rows(blob, recipe_id))

    def _chunk_rows(self, blob, recipe_id):
        body = self._page_blob(blob); self._recipe(recipe_id)
        head = self._db.execute('SELECT * FROM chunk_sets WHERE blob_sha=? AND recipe_id=?', (blob, recipe_id)).fetchone()
        if head is None:
            v.fail('chunk_set_missing')
        rows = [dict(row) for row in self._db.execute(
            'SELECT seq,position,text,text_sha FROM chunks WHERE blob_sha=? AND recipe_id=? ORDER BY seq',
            (blob, recipe_id))]
        rows = v.chunks(body, rows)
        if head['chunk_count'] != len(rows) or head['set_digest'] != v.chunk_digest(blob, recipe_id, rows):
            v.fail('chunk_set_invalid')
        return rows

    def put_chunk_set(self, blob_sha, recipe_id, rows):
        self._require_write()
        with self._session(write=True) as db:
            body = self._page_blob(blob_sha); self._recipe(recipe_id)
            rows = v.chunks(body, rows); key = v.chunk_digest(blob_sha, recipe_id, rows)
            old = db.execute('SELECT set_digest FROM chunk_sets WHERE blob_sha=? AND recipe_id=?', (blob_sha, recipe_id)).fetchone()
            if old:
                if self._chunks(blob_sha, recipe_id) != rows or old[0] != key:
                    v.fail('chunk_set_conflict')
            else:
                _insert(db, 'chunk_sets', dict(blob_sha=blob_sha, recipe_id=recipe_id, chunk_count=len(rows), set_digest=key))
                for row in rows:
                    _insert(db, 'chunks', dict(blob_sha=blob_sha, recipe_id=recipe_id, **row))
        return key

    def chunks(self, blob_sha, recipe_id):
        with self._session():
            return self._chunks(blob_sha, recipe_id)

    def _input_texts(self, blob, recipe_id, profile_id):
        body = self._page_blob(blob); self._profile(profile_id)
        rows = self._chunks(blob, recipe_id)
        title = v.markdown_title(body, blob)
        return [f'title: {title or "none"} | text: {r["text"]}' for r in rows]

    def _inputs(self, blob, recipe_id, profile_id):
        return self._page_memo('inputs', (blob, recipe_id, profile_id), lambda: [
            v.hash_bytes(text.encode('utf8')) for text in self._input_texts(blob, recipe_id, profile_id)])

    def embedding_inputs(self, blob_sha, recipe_id, profile_id):
        """Exact wiki-only synthetic inputs shared with set hash validation."""
        with self._session():
            return self._input_texts(blob_sha, recipe_id, profile_id)

    def vector_input_sha(self, blob_sha, recipe_id, profile_id, seq):
        v.integer(seq, 0, v.MAX_CHUNKS - 1)
        with self._session():
            inputs = self._inputs(blob_sha, recipe_id, profile_id)
            if seq >= len(inputs):
                v.fail('chunk_missing')
            return inputs[seq]

    def _vectors(self, blob, recipe_id, profile_id):
        spec = self._profile(profile_id)
        inputs = self._inputs(blob, recipe_id, profile_id)
        head = self._db.execute('SELECT * FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                                (blob, recipe_id, profile_id)).fetchone()
        if head is None:
            v.fail('embedding_set_missing')
        saved = list(self._db.execute(
            'SELECT seq,input_sha,embedding,provenance_digest FROM vectors '
            'WHERE blob_sha=? AND recipe_id=? AND profile_id=? ORDER BY seq', (blob, recipe_id, profile_id)))
        rows = v.embedding_rows([{k: row[k] for k in ('seq', 'input_sha', 'embedding')} for row in saved],
                                spec['dimension'], inputs)
        if (head['chunk_count'] != len(rows) or head['set_digest'] != v.embedding_digest(blob, recipe_id, profile_id, rows)
                or any(row['provenance_digest'] != v.vector_digest(blob, recipe_id, profile_id, data)
                       for row, data in zip(saved, rows))):
            v.fail('embedding_set_invalid')
        return rows

    def put_embedding_set(self, blob_sha, recipe_id, profile_id, rows):
        self._require_write()
        with self._session(write=True) as db:
            spec = self._profile(profile_id)
            inputs = self._inputs(blob_sha, recipe_id, profile_id)
            rows = v.embedding_rows(rows, spec['dimension'], inputs)
            key = v.embedding_digest(blob_sha, recipe_id, profile_id, rows)
            old = db.execute('SELECT set_digest FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                             (blob_sha, recipe_id, profile_id)).fetchone()
            if old:
                if self._vectors(blob_sha, recipe_id, profile_id) != rows or old[0] != key:
                    v.fail('embedding_set_conflict')
            else:
                _insert(db, 'embedding_sets', dict(blob_sha=blob_sha, recipe_id=recipe_id, profile_id=profile_id,
                                                  chunk_count=len(rows), set_digest=key))
                for row in rows:
                    _insert(db, 'vectors', dict(blob_sha=blob_sha, recipe_id=recipe_id, profile_id=profile_id,
                        **row, provenance_digest=v.vector_digest(blob_sha, recipe_id, profile_id, row)))
        return key

    def vectors(self, blob_sha, recipe_id, profile_id):
        with self._session():
            return self._vectors(blob_sha, recipe_id, profile_id)

    def _fingerprint(self, key, *, cache=None):
        if cache is not None and key in cache['fingerprints']:
            return cache['fingerprints'][key]
        v.identifier(key)
        row = self._db.execute('SELECT * FROM source_fingerprints WHERE fingerprint_id=?', (key,)).fetchone()
        if row is None:
            v.fail('source_fingerprint_missing')
        spec = v.fingerprint({k: row[k] for k in v.FINGERPRINT_FIELDS}, self._repository['gitObjectFormat'])
        if v.digest('memvane-source-fingerprint-v1', spec) != key:
            v.fail('source_fingerprint_invalid')
        if cache is None or spec['worktree_id'] not in cache['worktrees']:
            worktree = self._db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (spec['worktree_id'],)).fetchone()
            if worktree is None:
                v.fail('source_worktree_missing')
            record = _worktree_record(worktree, self._repository, self._policy)
            if cache is not None:
                cache['worktrees'][spec['worktree_id']] = (worktree, record)
        if cache is not None:
            cache['fingerprints'][key] = spec
        return spec

    def _put_fingerprint(self, db, spec, *, verify_only=False):
        spec = v.fingerprint(spec, self._repository['gitObjectFormat'])
        key = v.digest('memvane-source-fingerprint-v1', spec)
        worktree = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (spec['worktree_id'],)).fetchone()
        if worktree is None:
            v.fail('source_worktree_missing')
        _worktree_record(worktree, self._repository, self._policy)
        old = db.execute('SELECT fingerprint_id FROM source_fingerprints WHERE fingerprint_id=?', (key,)).fetchone()
        if old:
            if self._fingerprint(key) != spec:
                v.fail('source_fingerprint_conflict')
        elif verify_only:
            v.fail('migration_staged_missing')
        else:
            _insert(db, 'source_fingerprints', dict(fingerprint_id=key, **spec))
        return key

    def put_fingerprint(self, spec):
        self._require_write()
        with self._session(write=True) as db:
            return self._put_fingerprint(db, spec)

    def fingerprint(self, fingerprint_id):
        with self._session():
            return self._fingerprint(fingerprint_id)

    def _source_claims(self, spec, *, cache=None):
        fingerprints = []
        for source in spec['sources']:
            fp = self._fingerprint(source['fingerprint_id'], cache=cache)
            for span in source['spans']:
                if span.get('kind') == 'legacy-lines-v1':
                    if (spec['verification_kind'] != 'legacy_verified'
                            or fp['binding_state'] != 'legacy_unbound' or fp['head_oid'] is not None
                            or fp['tracked_state'] != 'unknown' or span['path'] != fp['path']
                            or span['sourceSha256'] != fp['source_sha']):
                        v.fail('legacy_span_binding_invalid')
                elif span.get('kind') == v.LINE_SPAN:
                    # Line numbers are bound to the observed fingerprint's path and
                    # SHA; the source bytes are not read, so byte_length is not a bound.
                    if (spec['verification_kind'] not in v.LINE_SPAN_KINDS
                            or fp['binding_state'] != 'observed' or span['path'] != fp['path']
                            or span['sourceSha256'] != fp['source_sha']):
                        v.fail('line_span_binding_invalid')
                elif span['end'] > fp['byte_length']:
                    v.fail('source_span_outside_fingerprint')
            if spec['verification_kind'] == 'compiled' and fp['binding_state'] != 'observed':
                v.fail('unbound_source_not_compiled')
            if spec['verification_kind'] == 'rebound' and (
                    fp['binding_state'] != 'observed' or fp['head_oid'] is None or fp['tracked_state'] == 'unknown'):
                v.fail('source_rebind_unknown')
            fingerprints.append(fp)
        return fingerprints

    def _match_rebound(self, new, old, *, cache=None):
        if (any(new[k] != old[k] for k in ('blob_sha', 'engine_identity', 'policy_digest'))
                or len(new['sources']) != len(old['sources']) or not old['sources']):
            v.fail('source_rebind_mismatch')
        for fresh, prior in zip(new['sources'], old['sources']):
            a = self._fingerprint(fresh['fingerprint_id'], cache=cache)
            b = self._fingerprint(prior['fingerprint_id'], cache=cache)
            if (b['binding_state'] != 'observed' or fresh['spans'] != prior['spans']
                    or any(a[k] != b[k] for k in ('path', 'source_sha', 'byte_length'))):
                v.fail('source_rebind_mismatch')

    def _attestation_one(self, key, *, cache=None):
        v.identifier(key)
        row = self._db.execute('SELECT * FROM card_attestations WHERE trust_digest=?', (key,)).fetchone()
        if row is None:
            v.fail('card_attestation_missing')
        saved = list(self._db.execute('SELECT * FROM attestation_sources WHERE trust_digest=? ORDER BY ordinal', (key,)))
        if len(saved) != row['source_count'] or [r['ordinal'] for r in saved] != list(range(len(saved))):
            v.fail('card_attestation_partial')
        sources = [dict(fingerprint_id=r['fingerprint_id'], spans=v.parse_canonical(r['spans_json'])) for r in saved]
        spec = v.attestation({**{k: row[k] for k in ('blob_sha', 'verification_kind', 'engine_identity', 'policy_digest')},
                              'receipt': v.parse_canonical(row['receipt_json']), 'sources': sources})
        if (v.digest('memvane-card-attestation-v1', spec) != key
                or row['provenance_digest'] != v.digest('memvane-card-provenance-v1', sources)):
            v.fail('card_attestation_invalid')
        if cache is None or spec['blob_sha'] not in cache['blobs']:
            self._blob(spec['blob_sha'], cache=cache)
            if cache is not None:
                cache['blobs'].add(spec['blob_sha'])
        self._source_claims(spec, cache=cache)
        return spec

    def _attestation(self, key, *, cache=None):
        seen, result, previous = set(), None, None
        while True:
            if key in seen:
                v.fail('card_attestation_cycle')
            seen.add(key)
            current = self._attestation_one(key, cache=cache)
            if result is None:
                result = current
            if previous is not None:
                self._match_rebound(previous, current, cache=cache)
            if current['verification_kind'] != 'rebound':
                return result
            previous, key = current, current['receipt']['prior_trust']

    def _put_attestation(self, db, spec, *, verify_only=False):
        spec = v.attestation(spec)
        key = v.digest('memvane-card-attestation-v1', spec)
        self._blob(spec['blob_sha']); self._source_claims(spec)
        if spec['verification_kind'] == 'rebound':
            self._match_rebound(spec, self._attestation(spec['receipt']['prior_trust']))
        old = db.execute('SELECT trust_digest FROM card_attestations WHERE trust_digest=?', (key,)).fetchone()
        if old:
            if self._attestation(key) != spec:
                v.fail('card_attestation_conflict')
        elif verify_only:
            v.fail('migration_staged_missing')
        else:
            _insert(db, 'card_attestations', dict(trust_digest=key,
                **{k: spec[k] for k in ('blob_sha', 'verification_kind', 'engine_identity', 'policy_digest')},
                provenance_digest=v.digest('memvane-card-provenance-v1', spec['sources']),
                receipt_json=v.canonical(spec['receipt']), source_count=len(spec['sources']), created_ns=time.time_ns()))
            for ordinal, source in enumerate(spec['sources']):
                _insert(db, 'attestation_sources', dict(trust_digest=key, ordinal=ordinal,
                    fingerprint_id=source['fingerprint_id'], spans_json=v.canonical(source['spans'])))
        return key

    def put_attestation(self, spec):
        self._require_write()
        with self._session(write=True) as db:
            return self._put_attestation(db, spec)

    def attestation(self, trust_digest):
        with self._session():
            return self._attestation(trust_digest)

    def verify(self):
        with self._session() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                v.fail('candidate_integrity_failed')
            for row in db.execute('SELECT * FROM worktrees'):
                _worktree_record(row, self._repository, self._policy)
            for row in db.execute('SELECT blob_sha FROM wiki_blobs'):
                self._blob(row[0])
            for row in db.execute('SELECT recipe_id FROM chunk_recipes'):
                self._recipe(row[0])
            for row in db.execute('SELECT profile_id FROM vector_profiles'):
                self._profile(row[0])
            for row in db.execute('SELECT blob_sha,recipe_id FROM chunk_sets'):
                self._chunks(*row)
            for row in db.execute('SELECT blob_sha,recipe_id,profile_id FROM embedding_sets'):
                self._vectors(*row)
            for row in db.execute('SELECT fingerprint_id FROM source_fingerprints'):
                self._fingerprint(row[0])
            for row in db.execute('SELECT trust_digest FROM card_attestations'):
                self._attestation(row[0])
        return True

    def counts(self):
        with self._session() as db:
            return {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                    for name in schema.TABLES}

    def close(self):
        self._db.close()
