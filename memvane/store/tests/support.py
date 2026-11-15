"""Store domain test helpers: synthetic blob and view stores over temporary checkouts.

Every case runs in its ``FixtureCase`` HOME. ``BlobStoreCase`` builds a store
beside a synthetic project (``.memvane`` settings, wiki cards and a raw note);
``GenerationCase`` adds the view store and a one-card manifest; ``FillCase``
publishes that manifest as the view head (format 4 fills the published head)
and supplies synthetic chunker, embedder and fence callbacks; ``VectorCase``
publishes complete vector coverage on top. ``SetCase`` is a bare blob store for
recipe, profile and set contracts. ``CheckoutCase`` drives real temporary git
repositories for the checkout capture contract.
"""
from __future__ import annotations

import contextlib
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import time
from unittest import mock
import uuid

from memvane.kernel.paths import canonical
from memvane.store.blob import blob_store
from memvane.store.checkout import checkout_view as view
from tests.support.case import FixtureCase

APP_ID = 0x4D564231
SETTINGS = {'name': 'blobfixture'}
CARD = b'# Shared Title\n\nA wiki card body.\n'
BEE = b'# Bee\nsecond card\n'
SCAN = '1' * 32
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def sha(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def recipe():
    return dict(input_recipe='blob-input-v1', tokenizer_sha='1' * 64,
                chunker_sha='2' * 64, window_tokens=900, overlap_tokens=135,
                window_search_tokens=200, offset_unit='utf16-code-unit',
                heading_rule='qmd-markdown-v1',
                document_template='title: {title} | text: {text}',
                synthetic_path='blobs/{blob_sha}.md')


def profile():
    return dict(model_id='synthetic-model', model_sha='3' * 64,
                embedding_fingerprint='4' * 64, dimension=2, metric='cosine',
                document_input_recipe='blob-input-v1',
                query_input_recipe='qmd-query-v1',
                query_template='task: search result | query: {text}')


def chunk(text, seq=0, position=0):
    return dict(seq=seq, position=position, text=text, text_sha=sha(text))


def clean_environ(prefixes=('GIT_', 'XDG_')):
    return {k: v for k, v in os.environ.items() if not k.startswith(prefixes)}


def corrupt_fts_block(path, table, *, remove=False):
    """Mutate one real FTS shadow block (``<table>_data``) in a disposable test database."""
    if not isinstance(table, str) or type(remove) is not bool:
        raise ValueError('invalid synthetic FTS mutation')
    name = table + '_data'
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT id,block FROM "' + name + '" ORDER BY id DESC LIMIT 1').fetchone()
        if row is None or not row[1]:
            raise AssertionError('synthetic FTS block missing')
        changed = row[1][:-1] + bytes([row[1][-1] ^ 1])
        action = ('DELETE FROM "' + name + '" WHERE id=?' if remove else
                  'UPDATE "' + name + '" SET block=? WHERE id=?')
        args = (row[0],) if remove else (changed, row[0])
        try:
            db.execute(action, args)
        except sqlite3.OperationalError as exc:
            if 'may not be modified' not in str(exc):
                raise
            db.rollback()
            cli = shutil.which('sqlite3')
            if cli is None:
                raise
            sql = ('DELETE FROM "' + name + '" WHERE id=' + str(row[0]) if remove else
                   'UPDATE "' + name + "\" SET block=X'" + changed.hex() + "' WHERE id=" + str(row[0]))
            subprocess.run([cli, '-batch', str(path), sql], check=True, capture_output=True, timeout=10)
        actual = db.execute('SELECT block FROM "' + name + '" WHERE id=?', (row[0],)).fetchone()
        if (remove and actual is not None) or (not remove and (actual is None or actual[0] != changed)):
            raise AssertionError('synthetic FTS mutation did not persist')


def clear_authorizer(db):
    """Lift a test authorizer; py3.9 ``sqlite3`` cannot take None (3.11+), so allow everything."""
    db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)


def drop_triggers_and(path, table, *statements):
    """Run ``statements`` on ``table`` of a disposable store with its immutable triggers lifted."""
    with closing(sqlite3.connect(str(path))) as db, db:
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                              (table,)).fetchall()
        for name, _ in triggers:
            db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for sql, params in statements:
            db.execute(sql, params)
        for _, ddl in triggers:
            db.execute(ddl)
        return len(triggers)


class BlobStoreCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.mod = blob_store
        self.Store, self.Error = self.mod.BlobStore, self.mod.BlobStoreError
        self.base = self.home
        env = mock.patch.dict(os.environ, {**clean_environ(), 'HOME': str(self.home)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.state = self.home / 'state' / 'blob'
        self.state.mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        self.db, self.scope = self.state / 'candidate.db', uuid.uuid4().hex
        self.root = self.project('plain')

    def project(self, name):
        root = self.home / 'projects' / name
        wiki = root / '.memvane' / 'wiki'
        (wiki / 'cards').mkdir(parents=True)
        (root / 'notes').mkdir()
        (root / '.memvane' / 'settings.json').write_text(json.dumps(SETTINGS), encoding='utf8')
        for meta in ('SCHEMA.md', 'index.md', 'log.md'):
            (wiki / meta).write_text('# ' + meta + '\n', encoding='utf8')
        (wiki / 'cards' / 'a.md').write_bytes(CARD)
        (root / 'notes' / 'src.md').write_text('# Source\nSECRET source body\n', encoding='utf8')
        return root

    def wiki(self, root=None):
        return (root or self.root) / '.memvane' / 'wiki'

    def git(self, cwd, *args):
        result = subprocess.run(
            ['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
             '-c', 'init.defaultBranch=main', *args], cwd=str(cwd),
            env={**os.environ, **GIT_ENV, 'GIT_CEILING_DIRECTORIES': str(self.base)},
            stdin=subprocess.DEVNULL, capture_output=True, encoding='utf8', timeout=30, check=False)
        self.assertEqual(result.returncode, 0, f'git {args}: {result.stderr}')

    def bind(self, root=None):
        repository = view.register_repository(str(root or self.root))
        return repository, view.register_worktree(str(root or self.root), repository)

    def track(self, store):
        def close():
            with contextlib.suppress(Exception):  # the test may already have closed it
                store.close()
        self.addCleanup(close)
        return store

    def create(self, root=None, path=None):
        repository, worktree = self.bind(root)
        store = self.Store.create(str(path or self.db), project_root=str(root or self.root),
                                  repository=repository, worktree=worktree, scope_id=self.scope)
        return self.track(store), repository, worktree['worktreeId']

    def reopen(self, repository, path=None):
        return self.track(self.Store.open(str(path or self.db), repository=repository,
                                          scope_id=self.scope))

    def refused(self, fn, *args, **kwargs):
        with self.assertRaises(self.Error) as caught:
            fn(*args, **kwargs)
        self.assertIsInstance(caught.exception, ValueError)
        reason = getattr(caught.exception, 'reason', None)
        self.assertTrue(isinstance(reason, str) and reason, 'BlobStoreError.reason code')
        return caught.exception

    def rejects_ingest(self, store, worktree_id, *paths):
        before = store.counts()
        for path in paths:
            with self.subTest(path=path):
                error = self.refused(store.ingest_card, worktree_id, path)
                self.assertNotIn('SECRET', str(error) + repr(error.args))
        self.assertEqual(store.counts(), before)


class GenerationCase(BlobStoreCase):
    """A view store over the plain project with one trusted card citing ``notes/src.md``."""

    def setUp(self):
        super().setUp()
        from memvane.store.view import view_store
        self.views = view_store
        self.Store = self.views.ViewStore

    def seeded(self, root=None):
        store, repository, wid = self.create(root, path=None if root is None else self.state / (root.name + '.db'))
        handle = store.capture_view(wid)
        return store, repository, handle

    def snapshot(self, store, handle, *, root=None, scan_id=SCAN, body=None,
                 source_sha=None, presence='present', zero=False, trust=True):
        root = root or self.root
        if body is not None:
            (root / '.memvane/wiki/cards/a.md').write_bytes(body)
        blob = store.ingest_card(handle.worktree_id, 'cards/a.md')
        raw = (root / 'notes/src.md').read_bytes()
        fp = store.put_fingerprint(dict(
            worktree_id=handle.worktree_id, branch_key=handle.branch_key,
            path='notes/src.md', source_sha=source_sha or hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw), head_oid=handle.head_oid,
            tracked_state='unknown' if handle.branch_key == 'non-git' else 'tracked',
            binding_state='observed', scan_id=scan_id, observed_ns=1))
        evidence = [] if zero else [dict(fingerprint_id=fp, spans=[dict(start=0, end=4)])]
        attestation = store.put_attestation(dict(
            blob_sha=blob, verification_kind='compiled', engine_identity='synthetic:codex',
            policy_digest=handle.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1', verdict='verified',
                         result_digest='e' * 64, prior_trust=None), sources=evidence)) if trust else None
        return dict(scan_id=scan_id, policy_digest=handle.policy_digest, revocation_epoch=0, complete=True,
                    sources=[] if zero else [dict(path='notes/src.md', fingerprint_id=fp, presence=presence)],
                    members=[dict(path='cards/a.md', blob_sha=blob, display_title='Shared',
                                  trust_digest=attestation)])

    def prepared(self, store, handle, manifest=None):
        head = store.head(handle)
        gen = store.prepare_generation(handle, manifest or self.snapshot(store, handle), expected_head=head)
        return gen, head

    def published(self, *, body=b'# Alpha\nhello original snapshot\n'):
        store, repository, h = self.seeded()
        m = self.snapshot(store, h, body=body)
        gen, base = self.prepared(store, h, m)
        store.publish_generation(h, gen, expected_head=base)
        return store, repository, h, gen

    def eligible(self, store, gen):
        row = store._db.execute('SELECT metadata_json FROM s_stage_items WHERE job_id=? AND path=?',
                                (gen[2:34], 'cards/a.md')).fetchone()
        return json.loads(row[0])['eligible']

    def generations(self, store):
        return store._db.execute('SELECT COUNT(*) FROM s_generations').fetchone()[0]

    def add_member(self, store, h, m, path, body, title):
        p = self.root / '.memvane/wiki' / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        blob = store.ingest_card(h.worktree_id, path)
        att = store.put_attestation(dict(
            blob_sha=blob, verification_kind='compiled', engine_identity='synthetic:codex',
            policy_digest=h.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1', verdict='verified', result_digest='e' * 64,
                         prior_trust=None),
            sources=[dict(fingerprint_id=m['sources'][0]['fingerprint_id'], spans=[dict(start=0, end=4)])]))
        m['members'].append(dict(path=path, blob_sha=blob, display_title=title, trust_digest=att))

    def pair(self, name):
        main, linked = self.project(name), self.home / 'projects' / (name + '-linked')
        for args in [('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture'),
                     ('worktree', 'add', '-q', '-b', 'side', str(linked))]:
            self.git(main, *args)
        store, repository, a = self.seeded(main)
        record = view.register_worktree(str(linked), repository)
        wid = store.register_worktree(str(linked), record)
        return store, main, linked, a, store.capture_view(wid)


class FillCase(GenerationCase):
    """``configured`` publishes the manifest as the view head; fills run over that head."""

    def setUp(self):
        super().setUp()
        self.calls = dict(chunker=[], embedder=[], fence=[])

    def lexical(self, store, handle, manifest):
        head = store.head(handle)
        gen = store.prepare_generation(handle, manifest, expected_head=head)
        return store.publish_generation(handle, gen, expected_head=head)

    def configured(self, count=1, *, body=None, publish=True):
        store, repository, handle = self.seeded()
        manifest = self.snapshot(store, handle, body=body)
        for i in range(1, count):
            self.add_member(store, handle, manifest, f'cards/{i:02}.md',
                            f'# Card {i}\nhello member {i}\n'.encode(), f'Card {i}')
        recipe_id = store.register_recipe(recipe())
        profile_id = store.register_profile(profile())
        if publish:
            self.lexical(store, handle, manifest)
        return store, repository, handle, manifest, recipe_id, profile_id

    def callbacks(self, store):
        def chunker(document, recipe_value):
            self.assertFalse(store._db.in_transaction, 'chunker runs outside SQLite transaction')
            self.assertTrue(self.calls['fence'], 'write fence precedes content production')
            self.assertEqual(document, store.document(Path(document['path']).stem))
            self.assertRegex(document['path'], r'^blobs/[0-9a-f]{64}\.md$')
            self.assertEqual(recipe_value, recipe())
            self.calls['chunker'].append(copy.deepcopy(document))
            return [chunk(document['body'])]

        def embedder(inputs, profile_value):
            self.assertFalse(store._db.in_transaction, 'embedder runs outside SQLite transaction')
            self.assertEqual(profile_value, profile())
            self.assertTrue(all(isinstance(value, str) for value in inputs))
            self.calls['embedder'].append(list(inputs))
            return [[1.0, 0.5] for _ in inputs]

        def fence():
            self.assertFalse(store._db.in_transaction, 'first write fence runs before transaction')
            self.calls['fence'].append(True)

        return dict(chunker=chunker, embedder=embedder, write_fence=fence)

    def fill(self, store, handle, manifest, recipe_id, profile_id, **kwargs):
        options = self.callbacks(store)
        options.update(kwargs)
        options.setdefault('expected_cursor',
                           store.fill_status(handle, manifest, recipe_id, profile_id)['cursor'])
        return store.fill_view(handle, manifest, recipe_id, profile_id, **options)

    def assert_status(self, result, cursor, complete):
        self.assertTrue({'job_id', 'cursor', 'complete'} <= set(result))
        self.assertIs(type(result['cursor']), int)
        self.assertEqual(result['cursor'], cursor)
        self.assertIs(result['complete'], complete)

    def bounded_refusal(self, fn, *args, **kwargs):
        error = self.refused(fn, *args, **kwargs)
        self.assertLessEqual(len(error.reason), 160, 'public refusal reason stays bounded')
        return error


class VectorCase(FillCase):
    """``ready`` publishes the lexical head, fills it and publishes complete coverage."""

    def publish(self, store, handle, manifest, recipe_id=None, profile_id=None):
        base = store.head(handle)
        vector_spec = None if recipe_id is None else dict(recipe_id=recipe_id, profile_id=profile_id)
        gen = store.prepare_generation(handle, manifest, expected_head=base, vector_spec=vector_spec)
        store.publish_generation(handle, gen, expected_head=base)
        return gen, store.pin_view(handle)

    def ready(self, count=1, *, lexical=False):
        store, repository, h, manifest, r, p = self.configured(count, body=b'# Alpha\nhello original snapshot\n')
        if lexical:
            return (store, repository, h, manifest, r, p, store.head(h)['gen_id'], store.pin_view(h))
        self.fill(store, h, manifest, r, p)
        gen, pin = self.publish(store, h, manifest, r, p)
        return store, repository, h, manifest, r, p, gen, pin

    def put_vectors(self, store, blob, recipe_id, profile_id, values, *, pieces=None):
        body = store.document(blob)['body']
        chunks = pieces or [chunk(body)]
        store.put_chunk_set(blob, recipe_id, chunks)
        store.put_embedding_set(blob, recipe_id, profile_id,
                                [dict(seq=i, input_sha=store.vector_input_sha(blob, recipe_id, profile_id, i),
                                      embedding=struct.pack('<' + 'f' * len(value), *value))
                                 for i, value in enumerate(values)])

    def assert_rows(self, rows, pin):
        for row in rows:
            self.assertEqual((row['gen_id'], row['view_id']), (pin.gen_id, pin.view_id))
            self.assertEqual(row['evidence'], 'commitment-only')
            self.assertIs(row['owner_verified'], False)
            self.assertTrue(isinstance(row['body'], str))
            self.assertTrue(isinstance(row['score'], (int, float)))

    def damage(self, table, sql, params=()):
        """Alter existing private fixture relations, then restore exact schema SQL."""
        self.assertTrue(drop_triggers_and(self.db, table, (sql, params)),
                        'fixture damage requires existing immutable triggers')


class SetCase(FixtureCase):
    """A bare blob store over a non-git project holding one card."""

    def setUp(self):
        super().setUp()
        self.Store, self.Error = blob_store.BlobStore, blob_store.BlobStoreError
        self.root = self.home / 'sets'
        self.root.mkdir()
        self.project = self.root / 'project'
        self.wiki = self.project / '.memvane' / 'wiki'
        self.wiki.mkdir(parents=True)
        (self.wiki.parent / 'settings.json').write_text(
            json.dumps({'name': 'fixture', 'skipPaths': []}), encoding='utf8')
        self.body = '# 고정\n별빛😀 둘째 문장\n'
        (self.wiki / 'card.md').write_bytes(self.body.encode('utf8'))
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.path = self.state / 'candidate.sqlite'
        self.repository = view.register_repository(str(self.project))
        self.worktree = view.register_worktree(str(self.project), self.repository)
        self.scope = 'f' * 32
        self.store = self.Store.create(self.path, project_root=str(self.project),
                                       repository=self.repository, worktree=self.worktree,
                                       scope_id=self.scope)
        self.addCleanup(self.store.close)
        self.blob = self.store.ingest_card(self.worktree['worktreeId'], 'card.md')

    def prepared(self):
        r = self.store.register_recipe(recipe())
        p = self.store.register_profile(profile())
        return r, p

    def vector(self, r, p, *, embedding=None):
        return dict(seq=0, input_sha=self.store.vector_input_sha(self.blob, r, p, 0),
                    embedding=embedding if embedding is not None else struct.pack('<ff', 1, 2))


# -- checkout capture --------------------------------------------------------

REAL_GIT = shutil.which('git')


def leaves(value):
    if isinstance(value, (dict, list)):
        for item in (value.values() if isinstance(value, dict) else value):
            yield from leaves(item)
    else:
        yield value


def replaced(value, old, new):
    if isinstance(value, dict):
        return {key: replaced(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [replaced(item, old, new) for item in value]
    return new if type(value) is type(old) and value == old else value


def canon(path) -> str:
    return str(canonical(path))


def flip(digest: str) -> str:
    return digest[:-1] + ('0' if digest[-1] != '0' else '1')


class CheckoutCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = (self.home / 'co').resolve()
        self.base.mkdir()
        clean = mock.patch.dict(os.environ, clean_environ(('GIT_',)), clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        self.env = {**os.environ, **GIT_ENV, 'GIT_CEILING_DIRECTORIES': str(self.base)}
        self.root = self.repo('project')

    def git(self, cwd, *args, ok=(0,)):
        self.assertTrue(Path(cwd).resolve().is_relative_to(self.base), cwd)
        result = subprocess.run(
            [REAL_GIT, '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
             '-c', 'init.defaultBranch=main', '-c', 'protocol.file.allow=always', *args],
            cwd=str(cwd), env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30, check=False)
        if result.returncode not in ok:
            raise AssertionError(f'git {args} failed: {result.stderr}')
        return result

    def out(self, cwd, *args):
        return self.git(cwd, *args).stdout.strip()

    def repo(self, name, *, commit=True):
        root = self.base / name
        root.mkdir(parents=True)
        self.git(root, 'init', '-q')
        if commit:
            self.commit(root, 'a.txt', 'one\n')
        return root

    def commit(self, root, name, text):
        (root / name).write_text(text)
        self.git(root, 'add', '--', name)
        self.git(root, 'commit', '-q', '-m', 'edit ' + name)
        return self.out(root, 'rev-parse', 'HEAD')

    def bind(self, root):
        repository = view.register_repository(str(root))
        return repository, view.register_worktree(str(root), repository)

    def cap(self, root, repository, worktree, **kwargs):
        return view.capture_view(str(root), repository, worktree, **kwargs)

    def refused(self, fn, *args, reason=None, **kwargs):
        with self.assertRaises(view.CheckoutUnavailable) as caught:
            fn(*args, **kwargs)
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertTrue(isinstance(caught.exception.reason, str) and caught.exception.reason)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)

    def no_match(self, capture, repository, worktree, **kwargs):
        self.assertIs(view.capture_matches(capture, repository, worktree, **kwargs), False)

    def fake_git(self, body):
        bindir = self.base / 'fakebin'
        bindir.mkdir(exist_ok=True)
        (bindir / 'git').write_text('#!/bin/sh\n' + body)
        (bindir / 'git').chmod(0o755)
        return mock.patch.dict(os.environ, {'PATH': str(bindir) + os.pathsep + os.environ['PATH']})

    def assert_no_process(self, pidfile):
        pids = [int(item) for item in pidfile.read_text().split()] if pidfile.exists() else []
        self.assertTrue(pids, 'fake git was never invoked')
        deadline = time.monotonic() + 5
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)  # existence probe only
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail(f'fake git process {pid} outlived the refusal')
                time.sleep(0.05)
