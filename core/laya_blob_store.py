"""Inactive candidate storage; no routes, index generations or source-body API.

The caller owns the explicit scope and candidate path. Wiki admission validates
stable bounded observations, not an atomic filesystem/Git snapshot. Evidence
records in this file do not grant operational trust or deletion authority.
"""
from __future__ import annotations

from contextlib import contextmanager
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

NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


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


def _observation(project_root, repository, worktree):
    import config
    import laya_index_host_route as routes
    import project_dir
    try:
        capture = checkout.capture_view(str(project_root), repository, worktree)
        root = Path(capture['projectRoot'])
        settings = project_dir.settings_path(root)
        if settings is None:
            v.fail('wiki_settings_missing')
        raw, _, _ = _regular(settings.parent, settings.name, 1024 * 1024)
        raw_config = json.loads(raw.decode('utf8'), object_pairs_hook=routes._pairs)
        if not isinstance(raw_config, dict):
            v.fail('wiki_binding_invalid')
        policy_config = config.normalize_config(config.expand_minimal_settings(raw_config, project_dir.CURRENT),
                                                project_dir.CURRENT)
        policy_digest = v.digest('memvane-candidate-policy-v1', policy_config)
        collections = routes._derived_collections({'schema': routes.SCHEMA_V2}, root, raw)
        roots = routes.selected_collection_roots(root, collections)
        wiki = Path(next(iter(routes.wiki_index_roots(collections).values())))
        fd = _dirfd(wiki)
        try:
            node = _node(os.fstat(fd))
        finally:
            os.close(fd)
        if not checkout.capture_matches(capture, repository, worktree):
            v.fail('checkout_changed')
        again, _, _ = _regular(settings.parent, settings.name, 1024 * 1024)
        if raw != again:
            v.fail('wiki_settings_changed')
        return {'project_root': str(root), 'settings_path': str(settings),
                'settings_sha': v.hash_bytes(raw), 'policy_digest': policy_digest, 'wiki_root': str(wiki),
                'wiki_dev': node['dev'], 'wiki_ino': node['ino'],
                'collections': roots, 'capture': capture}
    except BlobStoreError:
        raise
    except (OSError, ValueError, KeyError, StopIteration, checkout.CheckoutUnavailable):
        v.fail('wiki_binding_invalid')


def _worktree_row(project_root, repository, worktree):
    observation = _observation(project_root, repository, worktree)
    evidence = {k: val for k, val in observation.items() if k != 'capture'}
    evidence['record'] = worktree
    row = {'worktree_id': worktree['worktreeId'],
           'canonical_root': worktree['checkout']['path'],
           'git_admin_dir': worktree['gitDir']['path'] if worktree['gitDir'] else None,
           'root_dev': worktree['checkout']['dev'], 'root_ino': worktree['checkout']['ino'],
           'admin_dev': worktree['gitDir']['dev'] if worktree['gitDir'] else None,
           'admin_ino': worktree['gitDir']['ino'] if worktree['gitDir'] else None,
           'binding_digest': v.digest('memvane-blob-worktree-v1', evidence),
           'binding_json': v.canonical(evidence),
           'project_root': observation['project_root'], 'record_json': v.canonical(worktree),
           **{k: observation[k] for k in ('settings_path', 'settings_sha', 'policy_digest', 'wiki_root', 'wiki_dev', 'wiki_ino')}}
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
    v.exact(binding, ('project_root', 'settings_path', 'settings_sha', 'policy_digest', 'wiki_root',
                      'wiki_dev', 'wiki_ino', 'collections', 'record'))
    if (not isinstance(record, dict) or set(record) != checkout.WORKTREE_FIELDS
            or record.get('schema') != checkout.WORKTREE_SCHEMA
            or record.get('worktreeId') != row['worktree_id']
            or record.get('repoId') != repository['repoId']
            or record.get('repositoryDigest') != checkout._digest(repository)
            or not all(checkout._identity(record.get(k)) for k in ('project', 'checkout'))
            or (record.get('gitDir') is not None and not checkout._identity(record['gitDir']))
            or binding.get('record') != record
            or v.digest('memvane-blob-worktree-v1', binding) != row['binding_digest']):
        v.fail('worktree_evidence_invalid')
    expected = dict(project_root=record['project']['path'],
                    canonical_root=record['checkout']['path'], root_dev=record['checkout']['dev'],
                    root_ino=record['checkout']['ino'],
                    git_admin_dir=record['gitDir']['path'] if record['gitDir'] else None,
                    admin_dev=record['gitDir']['dev'] if record['gitDir'] else None,
                    admin_ino=record['gitDir']['ino'] if record['gitDir'] else None)
    if (any(row[k] != val for k, val in expected.items())
                or row['policy_digest'] != policy_digest
                or any(binding.get(k) != row[k] for k in
                   ('project_root', 'settings_path', 'settings_sha', 'policy_digest', 'wiki_root', 'wiki_dev', 'wiki_ino'))):
        v.fail('worktree_evidence_invalid')
    return record


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
            db = sqlite3.connect(temp.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=2)
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                schema.create(db)
                common = repository['commonGitDir'] or {}
                _insert(db, 'store_meta', dict(singleton=1, format=schema.FORMAT, schema_version=1,
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
        repository = v.repository(repository); v.identifier(scope_id, v.UUID)
        path = _database_path(path)
        db = None
        try:
            identity = _db_file(path)
            ro = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2, isolation_level=None)
            try:
                ro.row_factory = sqlite3.Row
                ro.execute('BEGIN')
                initial_meta = _header(ro, repository, scope_id)
            finally:
                ro.close()
            db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA recursive_triggers=ON')
            db.execute('PRAGMA synchronous=FULL')
            if _db_file(path) != identity:
                v.fail('candidate_file_changed')
            final_meta = _header(db, repository, scope_id)
            if final_meta != initial_meta:
                v.fail('candidate_binding_changed')
            obj = object.__new__(cls)
            obj._path, obj._identity, obj._repository, obj._scope = path, identity, repository, scope_id
            obj._db = db
            obj._incarnation, obj._policy = initial_meta['incarnation'], initial_meta['settings_policy_digest']
            return obj
        except BlobStoreError:
            if db is not None: db.close()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if db is not None: db.close()
            v.fail('candidate_open_failed')

    @contextmanager
    def _session(self, *, write=False):
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
                v.fail('candidate_busy' if 'locked' in str(exc) or 'busy' in str(exc) else 'candidate_io_failed')
            raise

    def register_worktree(self, project_root, worktree):
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

    def _blob(self, blob_sha):
        v.identifier(blob_sha)
        row = self._db.execute('SELECT blob_sha,byte_length FROM wiki_blobs WHERE blob_sha=?', (blob_sha,)).fetchone()
        if row is None:
            v.fail('blob_missing')
        v.integer(row['byte_length'], 0, v.MAX_BODY)
        raw = self._db.execute('SELECT substr(CAST(body AS BLOB),1,?) FROM wiki_blobs WHERE blob_sha=?',
                               (v.MAX_BODY + 1, blob_sha)).fetchone()[0]
        try:
            body = raw.decode('utf8')
        except (AttributeError, UnicodeError):
            v.fail('blob_invalid')
        if len(raw) != row['byte_length'] or v.hash_bytes(raw) != blob_sha or len(raw) > v.MAX_BODY:
            v.fail('blob_conflict')
        admission = self._db.execute('SELECT * FROM blob_admissions WHERE blob_sha=?', (blob_sha,)).fetchone()
        if admission is None:
            v.fail('blob_admission_missing')
        proof = v.parse_canonical(admission['proof_json'])
        v.exact(proof, ('blob_sha', 'worktree_id', 'path', 'file', 'binding_digest', 'settings_sha',
                        'wiki_root', 'wiki_identity', 'repository_digest', 'capture'))
        saved = self._db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (admission['worktree_id'],)).fetchone()
        if saved is None:
            v.fail('blob_admission_invalid')
        worktree = _worktree_record(saved, self._repository, self._policy)
        capture = proof['capture']
        if (proof.get('blob_sha') != blob_sha or proof['worktree_id'] != saved['worktree_id']
                or proof['binding_digest'] != saved['binding_digest']
                or proof['settings_sha'] != saved['settings_sha'] or proof['wiki_root'] != saved['wiki_root']
                or proof['wiki_identity'] != {'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']}
                or proof['repository_digest'] != worktree['repositoryDigest']
                or not checkout._capture_shape(capture)
                or capture['repoId'] != self._repository['repoId']
                or capture['worktreeId'] != saved['worktree_id']
                or capture['checkoutDigest'] != checkout._digest({k: val for k, val in capture.items() if k != 'checkoutDigest'})
                or any(capture['evidence'][k] != worktree[k] for k in ('project', 'checkout', 'gitDir'))
                or v.digest('memvane-wiki-admission-v1', proof) != admission['proof_digest']):
            v.fail('blob_admission_invalid')
        v.relative(proof['path'])
        v.exact(proof['file'], ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'))
        for number in proof['file'].values():
            v.integer(number)
        if proof['file']['size'] != row['byte_length']:
            v.fail('blob_admission_invalid')
        return body

    def ingest_card(self, worktree_id, path):
        v.identifier(worktree_id, v.UUID); v.relative(path)
        if not path.endswith('.md') or (len(path.split('/')) == 1 and path in ('SCHEMA.md', 'index.md', 'log.md')):
            v.fail('wiki_card_path_invalid')
        with self._session() as db:
            saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
            if saved is None:
                v.fail('worktree_missing')
            saved = dict(saved)
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
                         binding_digest=row['binding_digest'], settings_sha=row['settings_sha'],
                         wiki_root=saved['wiki_root'], wiki_identity=rootnode,
                         repository_digest=worktree['repositoryDigest'], capture=observation['capture'])
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
                if _current_file_node(saved['wiki_root'], path) != node:
                    v.fail('wiki_card_changed')
                settings = Path(saved['settings_path'])
                final_settings, _, _ = _regular(settings.parent, settings.name, 1024 * 1024)
                if v.hash_bytes(final_settings) != saved['settings_sha']:
                    v.fail('wiki_settings_changed')
            return sha
        except BlobStoreError:
            raise
        except (OSError, UnicodeError, checkout.CheckoutUnavailable):
            v.fail('wiki_card_unavailable')

    def document(self, blob_sha):
        with self._session():
            body = self._blob(blob_sha)
            path = f'blobs/{blob_sha}.md'
            return {'body': body, 'path': path, 'title': v.markdown_title(body, blob_sha)}

    def register_recipe(self, spec):
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
        body = self._blob(blob); self._recipe(recipe_id)
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
        with self._session(write=True) as db:
            body = self._blob(blob_sha); self._recipe(recipe_id)
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

    def _inputs(self, blob, recipe_id, profile_id):
        body = self._blob(blob); self._profile(profile_id)
        rows = self._chunks(blob, recipe_id)
        title = v.markdown_title(body, blob)
        return [v.hash_bytes(f'title: {title or "none"} | text: {r["text"]}'.encode('utf8')) for r in rows]

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

    def _fingerprint(self, key):
        v.identifier(key)
        row = self._db.execute('SELECT * FROM source_fingerprints WHERE fingerprint_id=?', (key,)).fetchone()
        if row is None:
            v.fail('source_fingerprint_missing')
        spec = v.fingerprint({k: row[k] for k in v.FINGERPRINT_FIELDS}, self._repository['gitObjectFormat'])
        if v.digest('memvane-source-fingerprint-v1', spec) != key:
            v.fail('source_fingerprint_invalid')
        worktree = self._db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (spec['worktree_id'],)).fetchone()
        if worktree is None:
            v.fail('source_worktree_missing')
        _worktree_record(worktree, self._repository, self._policy)
        return spec

    def put_fingerprint(self, spec):
        spec = v.fingerprint(spec, self._repository['gitObjectFormat'])
        key = v.digest('memvane-source-fingerprint-v1', spec)
        with self._session(write=True) as db:
            worktree = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (spec['worktree_id'],)).fetchone()
            if worktree is None:
                v.fail('source_worktree_missing')
            _worktree_record(worktree, self._repository, self._policy)
            old = db.execute('SELECT fingerprint_id FROM source_fingerprints WHERE fingerprint_id=?', (key,)).fetchone()
            if old:
                if self._fingerprint(key) != spec:
                    v.fail('source_fingerprint_conflict')
            else:
                _insert(db, 'source_fingerprints', dict(fingerprint_id=key, **spec))
        return key

    def fingerprint(self, fingerprint_id):
        with self._session():
            return self._fingerprint(fingerprint_id)

    def _source_claims(self, spec):
        fingerprints = []
        for source in spec['sources']:
            fp = self._fingerprint(source['fingerprint_id'])
            if any(span['end'] > fp['byte_length'] for span in source['spans']):
                v.fail('source_span_outside_fingerprint')
            if spec['verification_kind'] == 'compiled' and fp['binding_state'] != 'observed':
                v.fail('unbound_source_not_compiled')
            if spec['verification_kind'] == 'rebound' and (
                    fp['binding_state'] != 'observed' or fp['head_oid'] is None or fp['tracked_state'] == 'unknown'):
                v.fail('source_rebind_unknown')
            fingerprints.append(fp)
        return fingerprints

    def _match_rebound(self, new, old):
        if (any(new[k] != old[k] for k in ('blob_sha', 'engine_identity', 'policy_digest'))
                or len(new['sources']) != len(old['sources']) or not old['sources']):
            v.fail('source_rebind_mismatch')
        for fresh, prior in zip(new['sources'], old['sources']):
            a, b = self._fingerprint(fresh['fingerprint_id']), self._fingerprint(prior['fingerprint_id'])
            if (b['binding_state'] != 'observed' or fresh['spans'] != prior['spans']
                    or any(a[k] != b[k] for k in ('path', 'source_sha', 'byte_length'))):
                v.fail('source_rebind_mismatch')

    def _attestation_one(self, key):
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
        self._blob(spec['blob_sha']); self._source_claims(spec)
        return spec

    def _attestation(self, key):
        seen, result, previous = set(), None, None
        while True:
            if key in seen:
                v.fail('card_attestation_cycle')
            seen.add(key)
            current = self._attestation_one(key)
            if result is None:
                result = current
            if previous is not None:
                self._match_rebound(previous, current)
            if current['verification_kind'] != 'rebound':
                return result
            previous, key = current, current['receipt']['prior_trust']

    def put_attestation(self, spec):
        spec = v.attestation(spec)
        key = v.digest('memvane-card-attestation-v1', spec)
        with self._session(write=True) as db:
            self._blob(spec['blob_sha']); self._source_claims(spec)
            if spec['verification_kind'] == 'rebound':
                self._match_rebound(spec, self._attestation(spec['receipt']['prior_trust']))
            old = db.execute('SELECT trust_digest FROM card_attestations WHERE trust_digest=?', (key,)).fetchone()
            if old:
                if self._attestation(key) != spec:
                    v.fail('card_attestation_conflict')
            else:
                _insert(db, 'card_attestations', dict(trust_digest=key,
                    **{k: spec[k] for k in ('blob_sha', 'verification_kind', 'engine_identity', 'policy_digest')},
                    provenance_digest=v.digest('memvane-card-provenance-v1', spec['sources']),
                    receipt_json=v.canonical(spec['receipt']), source_count=len(spec['sources']), created_ns=time.time_ns()))
                for ordinal, source in enumerate(spec['sources']):
                    _insert(db, 'attestation_sources', dict(trust_digest=key, ordinal=ordinal,
                        fingerprint_id=source['fingerprint_id'], spans_json=v.canonical(source['spans'])))
        return key

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
