#!/usr/bin/env python3
"""PR 2 candidate Git blob store: Task 1 store and wiki admission contract.

Pins only the public ``laya_blob_store`` API. Each test imports the module in
setUp, so an absent module fails every case. HOME, state and projects live
under one realpath temp root; only the linked-worktree case runs fixture git,
sequentially with no global/system config. Refusals assert the broad
BlobStoreError, never a guessed reason string.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'core'))
import laya_checkout_view as view  # noqa: E402  (PR 1, present at base)

APP_ID, FORMAT = 0x4D564231, b'memvane-git-blob-candidate-v1'
SETTINGS = {'name': 'blobfixture'}
CARD = b'# Shared Title\n\nA wiki card body.\n'
BEE = b'# Bee\nsecond card\n'
API = {'create', 'open', 'register_worktree', 'ingest_card', 'document', 'counts', 'close',
       'register_recipe', 'register_profile', 'put_chunk_set', 'chunks', 'vector_input_sha',
       'put_embedding_set', 'vectors', 'put_fingerprint', 'fingerprint', 'put_attestation',
       'attestation', 'verify'}
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'XDG_'))}
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


class BlobStoreCase(unittest.TestCase):
    def setUp(self):
        try:
            self.mod = importlib.import_module('laya_blob_store')
        except ModuleNotFoundError as exc:
            if exc.name != 'laya_blob_store':
                raise
            self.fail('core/laya_blob_store.py is absent: PR 2 candidate blob store not implemented')
        self.Store, self.Error = self.mod.BlobStore, self.mod.BlobStoreError
        temp = tempfile.TemporaryDirectory(prefix='laya-blob-store-')
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        env = mock.patch.dict(os.environ, {**CLEAN_ENV, 'HOME': str(self.home)}, clear=True)
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


class BlobStoreChecks(BlobStoreCase):
    def test_01_separate_application_create_reopen_and_no_clobber(self):
        store, repository, wid = self.create()
        sha = store.ingest_card(wid, 'cards/a.md')
        document, counts = store.document(sha), store.counts()
        self.assertEqual(json.loads(json.dumps(counts)), counts)
        self.assertTrue(all(isinstance(k, str) and type(v) is int for k, v in counts.items()))
        self.assertTrue(any(v > 0 for v in counts.values()), counts)
        store.close()
        info = self.db.lstat()
        self.assertEqual((info.st_mode & 0o777, info.st_nlink, self.state.stat().st_mode & 0o777),
                         (0o600, 1, 0o700))
        data = self.db.read_bytes()
        self.assertEqual(data[:16], b'SQLite format 3\x00')
        self.assertEqual((int.from_bytes(data[68:72], 'big'), int.from_bytes(data[60:64], 'big')),
                         (APP_ID, 1))
        self.assertIn(FORMAT, data)
        again = self.reopen(repository)
        self.assertEqual((again.document(sha), again.counts()), (document, counts))
        again.close()
        self.refused(self.create)
        self.assertEqual(self.db.read_bytes(), data)
        foreign, text = self.state / 'foreign.db', self.state / 'text.db'
        with contextlib.closing(sqlite3.connect(foreign)) as connection:
            connection.executescript('PRAGMA user_version=6; CREATE TABLE cards(path TEXT);')
        text.write_bytes(b'not a database\n')
        for path in (foreign, text):
            path.chmod(0o600)
            before = path.read_bytes()
            with self.subTest(path=path.name):
                self.refused(self.create, path=path)
                self.refused(self.reopen, repository, path=path)
                self.assertEqual(path.read_bytes(), before)

    def test_02_exact_bom_crlf_and_utf8_bytes_round_trip(self):
        store, _, wid = self.create()
        variants = {'cards/bom.md': b'\xef\xbb\xbf# Caf\xc3\xa9\r\nline \xe2\x9c\x93\r\n',
                    'cards/crlf.md': b'# Caf\xc3\xa9\r\nline \xe2\x9c\x93\r\n',
                    'cards/lf.md': b'# Caf\xc3\xa9\nline \xe2\x9c\x93\n',
                    'cards/nfd.md': '# Café\nline ✓\n'.encode('utf8')}
        for name, raw in variants.items():
            (self.wiki() / name).write_bytes(raw)
            with self.subTest(name=name):
                sha = store.ingest_card(wid, name)
                self.assertEqual(sha, sha256(raw))
                self.assertEqual(store.document(sha)['body'], raw.decode('utf8'))
        self.assertEqual(len({sha256(raw) for raw in variants.values()}), len(variants))
        self.assertEqual((self.wiki() / 'cards/bom.md').read_bytes(), variants['cards/bom.md'])

    def test_03_same_body_other_filename_shares_blob_and_synthetic_document(self):
        store, _, wid = self.create()
        (self.wiki() / 'deep/nest').mkdir(parents=True)
        others = ('cards/renamed-elsewhere.md', 'deep/nest/third.md')
        for name in others:
            (self.wiki() / name).write_bytes(CARD)
        first = store.ingest_card(wid, 'cards/a.md')
        document, counts = store.document(first), store.counts()
        for name in others:
            self.assertEqual(store.ingest_card(wid, name), first)
            self.assertEqual(store.document(first), document)
        self.assertEqual(store.counts(), counts)
        self.assertEqual(set(document), {'body', 'path', 'title'})
        self.assertEqual((document['body'], document['path']),
                         (CARD.decode('utf8'), 'blobs/' + first + '.md'))
        self.assertTrue(isinstance(document['title'], str) and document['title'])
        for original in ('cards', 'renamed', 'third', 'nest', str(self.root)):
            self.assertNotIn(original, document['path'] + document['title'])

    def test_04_raw_absolute_traversal_and_non_wiki_paths_are_refused(self):
        store, _, wid = self.create()
        (self.wiki() / '.hidden').mkdir()
        for name in ('.hidden/a.md', 'cards/.dot.md', 'cards/plain.txt'):
            (self.wiki() / name).write_bytes(CARD)
        card = self.wiki() / 'cards/a.md'
        self.rejects_ingest(store, wid, str(card), card, str(self.root / 'notes/src.md'),
                            '../../notes/src.md', '../settings.json', 'cards/../cards/a.md',
                            './cards/a.md', 'cards/./a.md', 'cards//a.md', '/cards/a.md',
                            'cards', 'cards/', '', 'cards/a.md\x00', 'cards/missing.md',
                            '.hidden/a.md', 'cards/.dot.md', 'cards/plain.txt')
        self.rejects_ingest(store, 'f' * 32, 'cards/a.md')
        public = {name for name in dir(self.Store)
                  if not name.startswith('_') and callable(getattr(self.Store, name))}
        self.assertLessEqual(public, API, 'no raw ingestion or body insertion API')
        self.assertEqual(store.ingest_card(wid, 'cards/a.md'), sha256(CARD))

    def test_05_root_meta_pages_excluded_but_nested_names_admitted(self):
        store, _, wid = self.create()
        nested = self.wiki() / 'topic'
        nested.mkdir()
        metas = ('SCHEMA.md', 'index.md', 'log.md')
        for meta in metas:
            (nested / meta).write_bytes(b'# Nested ' + meta.encode('utf8') + b'\n')
        self.rejects_ingest(store, wid, *metas)
        for meta in metas:
            with self.subTest(meta=meta):
                raw = (nested / meta).read_bytes()
                sha = store.ingest_card(wid, 'topic/' + meta)
                self.assertEqual((sha, store.document(sha)['body']), (sha256(raw), raw.decode('utf8')))

    def test_06_leaf_and_intermediate_symlinks_are_refused(self):
        store, _, wid = self.create()
        wiki, elsewhere = self.wiki(), self.home / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'x.md').write_bytes(CARD)
        os.symlink('a.md', wiki / 'cards/leaf.md')
        os.symlink(self.root / 'notes/src.md', wiki / 'cards/outside.md')
        os.symlink(wiki / 'cards', wiki / 'alias')
        os.symlink(elsewhere, wiki / 'ext')
        self.rejects_ingest(store, wid, 'cards/leaf.md', 'cards/outside.md', 'alias/a.md', 'ext/x.md')
        self.assertEqual(store.ingest_card(wid, 'cards/a.md'), sha256(CARD))

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_07_unsafe_database_permissions_and_parents_are_refused(self):
        loose = self.home / 'loose'
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        self.refused(self.create, path=loose / 'c.db')
        os.symlink(self.state, self.home / 'state-link')
        self.refused(self.create, path=self.home / 'state-link' / 'c.db')
        self.assertEqual((list(loose.iterdir()), (self.state / 'c.db').exists()), ([], False))
        store, repository, _ = self.create()
        store.close()
        alias, hard, wal = self.state / 'alias.db', self.state / 'hard.db', Path(str(self.db) + '-wal')
        os.symlink(self.db, alias)
        self.refused(self.reopen, repository, path=alias)
        alias.unlink()
        unsafe = (('mode', lambda: self.db.chmod(0o644), lambda: self.db.chmod(0o600)),
                  ('parent', lambda: self.state.chmod(0o750), lambda: self.state.chmod(0o700)),
                  ('hardlink', lambda: os.link(self.db, hard), hard.unlink),
                  ('sidecar', lambda: os.symlink(self.home / 'evil-wal', wal), wal.unlink))
        for label, apply, restore in unsafe:
            with self.subTest(unsafe=label):
                apply()
                try:
                    self.refused(self.reopen, repository)
                finally:
                    restore()
        self.reopen(repository).close()

    def test_08_wrong_repository_or_scope_reopen_is_refused(self):
        store, repository, _ = self.create()
        store.close()
        before = self.db.read_bytes()
        other, _ = self.bind(self.project('other'))
        for bad in (view.register_repository(str(self.root)), other,
                    dict(repository, repoId='0' * 32), dict(repository, extra=True)):
            with self.subTest(repository=bad['repoId']):
                self.refused(self.Store.open, str(self.db), repository=bad, scope_id=self.scope)
        for scope in (uuid.uuid4().hex, self.scope[:-1], '', None):
            with self.subTest(scope=scope):
                self.refused(self.Store.open, str(self.db), repository=repository, scope_id=scope)
        self.assertEqual(self.db.read_bytes(), before)
        self.reopen(repository).close()

    def test_09_invalid_utf8_cards_are_refused_without_echoing_bytes(self):
        store, _, wid = self.create()
        bad = {'cards/ff.md': b'# x\nSECRET \xff\xfe\n', 'cards/overlong.md': b'# x\nSECRET \xc0\xaf\n',
               'cards/surrogate.md': b'# x\nSECRET \xed\xa0\x80\n',
               'cards/truncated.md': b'# x\nSECRET \xe2\x9c'}
        for name, raw in bad.items():
            (self.wiki() / name).write_bytes(raw)
        self.rejects_ingest(store, wid, *bad)

    def test_10_changed_settings_invalidate_wiki_admission(self):
        store, _, wid = self.create()
        store.ingest_card(wid, 'cards/a.md')
        (self.wiki() / 'cards/b.md').write_bytes(BEE)
        settings = self.root / '.memvane/settings.json'
        legacy = {'name': 'blobfixture', 'collections': ['blobfixture-wiki'],
                  'collectionPaths': {'blobfixture-wiki': '.memvane/wiki'}}
        variants = (dict(legacy, collectionRoles={'blobfixture-wiki': 'source'}),
                    dict(legacy, collectionRoles={'blobfixture-wiki': 'raw'}),
                    dict(legacy, collectionPaths={'blobfixture-wiki': 'notes'},
                         collectionRoles={'blobfixture-wiki': 'wiki'}), '{not json', None)
        for value in variants:
            with self.subTest(settings=value):
                if value is None:
                    settings.unlink()
                else:
                    settings.write_text(value if isinstance(value, str) else json.dumps(value),
                                        encoding='utf8')
                self.rejects_ingest(store, wid, 'cards/b.md', 'cards/a.md')

    def test_11_replaced_wiki_directory_identity_is_refused(self):
        store, repository, wid = self.create()
        store.ingest_card(wid, 'cards/a.md')
        wiki = self.wiki()
        os.rename(wiki, wiki.with_name('wiki-retired'))  # old inode stays alive, path is reused
        (wiki / 'cards').mkdir(parents=True)
        (wiki / 'cards/a.md').write_bytes(CARD)
        (wiki / 'cards/b.md').write_bytes(BEE)
        self.rejects_ingest(store, wid, 'cards/a.md', 'cards/b.md')
        store.close()
        self.rejects_ingest(self.reopen(repository), wid, 'cards/a.md', 'cards/b.md')

    def test_12_linked_worktree_shares_blob_and_other_clone_is_refused(self):
        main, projects = self.project('gitmain'), self.home / 'projects'
        linked, clone = projects / 'linked', projects / 'clone'
        for args in (('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture'),
                     ('worktree', 'add', '-q', '-b', 'side', str(linked))):
            self.git(main, *args)
        self.git(projects, 'clone', '-q', str(main), str(clone))
        store, repository, wid = self.create(main)
        linked_record = view.register_worktree(str(linked), repository)
        linked_id = store.register_worktree(str(linked), linked_record)
        self.assertEqual(linked_id, linked_record['worktreeId'])
        sha = store.ingest_card(wid, 'cards/a.md')
        counts = store.counts()
        self.assertEqual(store.ingest_card(linked_id, 'cards/a.md'), sha)
        self.assertEqual((store.counts(), store.document(sha)['path']), (counts, 'blobs/' + sha + '.md'))
        _, clone_record = self.bind(clone)
        self.refused(store.register_worktree, str(clone), clone_record)
        self.rejects_ingest(store, clone_record['worktreeId'], 'cards/a.md')

    def test_13_same_digest_different_bytes_is_a_conflict(self):
        store, _, wid = self.create()
        old = store.ingest_card(wid, 'cards/a.md')
        (self.wiki() / 'cards/b.md').write_bytes(BEE)
        values = importlib.import_module('laya_blob_values')
        real_hash = values.hash_bytes
        with mock.patch.object(values, 'hash_bytes', side_effect=lambda b: old if b == BEE else real_hash(b)):
            error = self.refused(store.ingest_card, wid, 'cards/b.md')
            self.assertEqual(error.reason, 'blob_conflict')
        self.assertEqual(store.document(old)['body'], CARD.decode('utf8'))

    def test_14_card_replaced_after_read_is_not_admitted(self):
        store, _, wid = self.create()
        before = store.counts()
        original = self.mod._regular
        fired = False
        def read(root, path, limit):
            nonlocal fired
            result = original(root, path, limit)
            if path == 'cards/a.md' and not fired:
                fired = True
                replacement = self.wiki() / 'cards/replacement.tmp'
                replacement.write_bytes(BEE)
                os.replace(replacement, self.wiki() / path)
            return result
        with mock.patch.object(self.mod, '_regular', side_effect=read):
            self.refused(store.ingest_card, wid, 'cards/a.md')
        self.assertTrue(fired)
        self.assertEqual(store.counts(), before)

    def test_15_settings_changed_during_card_read_are_refused(self):
        store, _, wid = self.create()
        before = store.counts()
        original = self.mod._regular
        def read(root, path, limit):
            result = original(root, path, limit)
            if path == 'cards/a.md':
                (self.root / '.memvane/settings.json').write_text(
                    json.dumps(dict(SETTINGS, skipPaths=['notes'])), encoding='utf8')
            return result
        with mock.patch.object(self.mod, '_regular', side_effect=read):
            self.refused(store.ingest_card, wid, 'cards/a.md')
        self.assertEqual(store.counts(), before)

    def test_16_real_v6_database_is_unchanged_on_candidate_refusal(self):
        import laya_index_a
        path = self.state / 'v6.db'
        index = laya_index_a.LayaIndexA(path, create=True)
        index.close()
        repository, _ = self.bind()
        before = path.read_bytes()
        self.refused(self.reopen, repository, path=path)
        self.assertEqual(path.read_bytes(), before)

    def test_17_oversized_card_is_refused_before_admission(self):
        store, _, wid = self.create()
        (self.wiki() / 'cards/large.md').write_bytes(b'x' * (4 * 1024 * 1024 + 1))
        self.rejects_ingest(store, wid, 'cards/large.md')

    def test_18_sql_rejects_invalid_worktree_identity_fields(self):
        store, _, _ = self.create()
        with sqlite3.connect(self.db) as db:
            db.row_factory = sqlite3.Row
            original = dict(db.execute('SELECT * FROM worktrees').fetchone())
            for key, value in [('worktree_id', 'not-uuid'), ('binding_digest', 'not-sha')]:
                row = dict(original, worktree_id=uuid.uuid4().hex,
                           canonical_root=original['canonical_root'] + '-other',
                           binding_digest='f' * 64)
                row[key] = value
                try:
                    with self.subTest(key=key):
                        with self.assertRaises(sqlite3.Error):
                            db.execute('INSERT INTO worktrees (' + ','.join(row) + ') VALUES (' +
                                       ','.join('?' for _ in row) + ')', tuple(row.values()))
                finally:
                    db.rollback()

    def pair_for_policy(self, name):
        main, linked = self.project(name), self.home / 'projects' / (name + '-linked')
        for args in [('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture'),
                     ('worktree', 'add', '-q', '-b', 'side', str(linked))]:
            self.git(main, *args)
        store, repository, _ = self.create(main)
        return store, repository, linked

    def test_19_different_worktree_skip_policy_is_not_combined(self):
        store, repository, linked = self.pair_for_policy('different-policy')
        (linked / '.memvane/settings.json').write_text(
            json.dumps(dict(SETTINGS, skipPaths=['notes'])), encoding='utf8')
        record = view.register_worktree(str(linked), repository)
        before = store.counts()
        self.refused(store.register_worktree, str(linked), record)
        self.assertEqual(store.counts(), before)

    def test_20_same_semantic_policy_can_use_different_json_spelling(self):
        store, repository, linked = self.pair_for_policy('same-policy')
        (linked / '.memvane/settings.json').write_text(
            '{ "skipPaths": [], "name": "blobfixture" }\n', encoding='utf8')
        record = view.register_worktree(str(linked), repository)
        self.assertEqual(store.register_worktree(str(linked), record), record['worktreeId'])

    def test_21_invalid_database_path_has_bounded_api_error(self):
        repository, worktree = self.bind()
        for path in (None, True, 123, object()):
            for method, kwargs in [(self.Store.create, dict(project_root=str(self.root), worktree=worktree)),
                                   (self.Store.open, {})]:
                with self.subTest(path_type=type(path).__name__, method=method.__name__):
                    self.refused(method, path, repository=repository, scope_id=self.scope, **kwargs)

    def test_22_open_handle_rejects_changed_store_incarnation(self):
        store, _, _ = self.create()
        with sqlite3.connect(self.db) as db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='store_meta_update'").fetchone()[0]
            db.execute('DROP TRIGGER store_meta_update')
            db.execute('UPDATE store_meta SET incarnation=?', (uuid.uuid4().hex,))
            db.execute(trigger); db.commit()
        self.refused(store.counts)

    def test_23_legacy_folder_spelling_has_same_settings_policy(self):
        store, repository, linked = self.pair_for_policy('legacy-policy')
        os.rename(linked / '.memvane', linked / '.auto-context')
        record = view.register_worktree(str(linked), repository)
        wid = store.register_worktree(str(linked), record)
        self.assertEqual(store.ingest_card(wid, 'cards/a.md'), sha256(CARD))


if __name__ == '__main__':
    unittest.main()
