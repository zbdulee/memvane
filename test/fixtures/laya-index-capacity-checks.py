"""Disposable durable binding sidecar checks.

Synthetic temp-dir corpora, a temp A-int DB, a temp claim queue and a temp
SQLite binding store only; no operating DB, source, queue or store. Crash
cases run a child Python process against the temp store only.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import laya_index_capacity as cap
from laya_index_a import IndexContractError, InjectedFault, LayaIndexA
from laya_index_capacity import (GenerationBindingStore, capture_bound_generation_binding,
                                 enqueue_bound_generation, load_pinned_binding,
                                 open_binding_store, open_pinned_candidate_query,
                                 persist_generation_binding, search_pinned_candidates)
from laya_index_claim_queue import ClaimQueue
from laya_source_catalog import (CandidateQuery, CatalogPending,
                                 GenerationSourceBinding, LayaSourceCatalog,
                                 capture_generation_binding, publish_from_catalog,
                                 stage_from_catalog)

SCOPE = 'scope-capacity'
CLAIM = 'c' * 64

# Dies inside ``put`` at the given fault point, as a crash would.
PUT_CHILD = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import laya_index_capacity as cap
from laya_source_catalog import GenerationSourceBinding
path, scope, incarnation, roots, record, point = sys.argv[2:]
store = cap.GenerationBindingStore(path, scope=scope, db_incarnation=incarnation,
                                   roots=json.loads(roots))
def die(fault_at, where):
    if fault_at == where:
        os._exit(70)
cap.GenerationBindingStore._inject = staticmethod(die)
store.put(GenerationSourceBinding.from_record(json.loads(record)), fault_at=point)
os._exit(0)
'''

# Spills an uncommitted transaction into the DB file, then dies: a hot journal.
HOT_JOURNAL_CHILD = r'''
import os, sqlite3, sys
path, scope, incarnation = sys.argv[1:]
db = sqlite3.connect(path, isolation_level=None)
db.execute('PRAGMA cache_size=10')
db.execute('BEGIN IMMEDIATE')
for i in range(400):
    digest = '%064x' % i
    db.execute('INSERT INTO bindings VALUES(?,?,?,?,?)',
               (scope, 'g-' + '0' * 32 + '-b' + digest, incarnation, digest, 'x' * 4000))
os._exit(70)
'''


def drive(catalog, **budget):
    for _ in range(10_000):
        result = catalog.step(**budget)
        if result['status'] != 'pending':
            return result
    raise AssertionError('scan did not settle')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canon(binding):
    return cap._canonical(binding.as_record()).decode('ascii')


def with_root(binding, index=0, **change):
    """``binding`` with one root identity changed (name, root, dev or ino)."""
    name, root, dev, ino = binding.roots[index]
    values = {'name': name, 'root': root, 'dev': dev, 'ino': ino, **change}
    roots = list(binding.roots)
    roots[index] = (values['name'], values['root'], values['dev'], values['ino'])
    return dataclasses.replace(binding, roots=tuple(roots))


class CapacityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-index-capacity-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.plans = self.base / 'src' / 'plans'
        self.wiki = self.base / 'src' / '.auto-context' / 'wiki'
        self.plans.mkdir(parents=True)
        self.wiki.mkdir(parents=True)
        self.roots = {'plans': str(self.plans), 'wiki': str(self.wiki)}
        self.state = self.base / 'state'
        self.state.mkdir()
        self.index = LayaIndexA(self.state / 'index.sqlite', create=True)
        self.addCleanup(self.index.close)

    @staticmethod
    def write(root, rel, text):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def scanned_catalog(self, roots=None, name='catalog.sqlite'):
        catalog = LayaSourceCatalog(self.state / name, roots or self.roots, SCOPE,
                                    create=True)
        self.addCleanup(catalog.close)
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog)['status'], 'complete')
        return catalog

    def rescan(self, catalog):
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog)['status'], 'complete')

    def capture(self, catalog, nonce=None):
        return capture_bound_generation_binding(self.index, catalog,
                                                catalog.require_complete(), nonce=nonce)

    def capture_legacy(self, catalog, gen):
        """Unsuffixed name, as every existing caller allocates today."""
        return capture_generation_binding(self.index, catalog, catalog.require_complete(), gen)

    def stage(self, catalog, binding, base):
        self.assertEqual(stage_from_catalog(self.index, catalog, binding.generation,
                                            base=base, claim_digest=CLAIM, claim_epoch=0,
                                            binding=binding), 'prepared')

    def publish(self, catalog, binding, base):
        gen = binding.generation
        self.assertEqual(publish_from_catalog(self.index, catalog, base, gen,
                                              'claim-' + gen, claim_digest=CLAIM,
                                              claim_epoch=0, binding=binding),
                         'published')

    def publish_persisted(self, catalog, store, base):
        """Capture (allocating the bound name), stage, persist, then publish."""
        binding = self.capture(catalog)
        self.stage(catalog, binding, base)
        self.assertEqual(persist_generation_binding(store, self.index, binding)['status'],
                         'stored')
        self.publish(catalog, binding, base)
        return binding

    def basic(self):
        """alpha/beta under plans, one wiki card; g1 persisted, then published."""
        self.alpha = self.write(self.plans, 'alpha.md', '# Alpha\nalpha topic body\n')
        self.beta = self.write(self.plans, 'beta.md', '# Beta\nbeta body\n')
        self.card = self.write(self.wiki, 'card.md', '# Card\nalpha wiki card\n')
        catalog = self.scanned_catalog()
        store = open_binding_store(self.state / 'bindings.sqlite', self.index, catalog,
                                   create=True)
        self.addCleanup(store.close)
        return catalog, self.publish_persisted(catalog, store, None), store

    def reopen(self, path, **identity):
        values = {'scope': SCOPE, 'roots': self.roots, **identity}
        if 'db_incarnation' not in values:
            values['db_incarnation'] = self.index.incarnation()
        store = GenerationBindingStore(path, **values)
        self.addCleanup(store.close)
        return store

    def worktree_b(self):
        shutil.copytree(self.base / 'src', self.base / 'src-b')
        return {'plans': str(self.base / 'src-b' / 'plans'),
                'wiki': str(self.base / 'src-b' / '.auto-context' / 'wiki')}

    @contextlib.contextmanager
    def pinned(self, index=None):
        index = index or self.index
        token, _gen = index.pin(SCOPE)
        try:
            yield token
        finally:
            index.release(token)

    def assert_stale(self, call, detail):
        with self.assertRaisesRegex(IndexContractError, 'index_stale') as caught:
            call()
        self.assertNotIsInstance(caught.exception, CatalogPending)
        self.assertIn(detail, str(caught.exception.__cause__))

    @staticmethod
    def raw_rows(path):
        with contextlib.closing(sqlite3.connect(path)) as db:
            return db.execute('SELECT * FROM bindings ORDER BY generation').fetchall()

    @staticmethod
    def raw(path, *statements):
        with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
            for sql in statements:
                db.execute(sql)

    @staticmethod
    def tamper(path, *statements):
        """Raw writer that drops the triggers, edits, and restores them unchanged.

        It models anyone who can write the file: the store schema still
        matches afterwards, so only content checks can catch the edit.
        """
        with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
            triggers = db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
            db.execute('BEGIN IMMEDIATE')
            for name, _sql in triggers:
                db.execute(f'DROP TRIGGER {name}')
            for sql, params in statements:
                db.execute(sql, params)
            for _name, sql in triggers:
                db.execute(sql)
            db.execute('COMMIT')
            assert cap._schema_rows(db) == cap._EXPECTED_SCHEMA

    def run_child(self, code, *args):
        return subprocess.run([sys.executable, '-I', '-B', '-c', code, *map(str, args)],
                              capture_output=True, text=True, timeout=120)


class StoreTests(CapacityCase):
    def test_persist_and_load_round_trip(self):
        _catalog, binding, store = self.basic()
        gen, incarnation = binding.generation, binding.db_incarnation
        self.assertRegex(gen, r'\Ag-[0-9a-f]{32}-b[0-9a-f]{64}\Z')
        record = binding.as_record()
        del record['generation']
        digest = sha(cap.NAME_DOMAIN + json.dumps(record, sort_keys=True,
                                                  separators=(',', ':')).encode('ascii'))
        self.assertEqual(gen[-64:], digest)
        self.assertEqual(cap.binding_digest(binding), digest)

        self.assertEqual(store.get(SCOPE, gen, incarnation), binding)
        unstored = cap.bound_generation_name(binding)  # same digest, fresh nonce
        self.assertNotEqual(unstored, gen)
        self.assertIsNone(store.get(SCOPE, unstored, incarnation))
        self.assert_stale(lambda: store.get('other', gen, incarnation),
                          'binding key differs from store identity')
        self.assert_stale(lambda: store.get(SCOPE, gen, 'f' * 32),
                          'binding key differs from store identity')

        # One primary-key search per lookup; never a scan of the store.
        plan = ' '.join(str(row[-1]) for row in store.db.execute(
            'EXPLAIN QUERY PLAN ' + cap._LOOKUP_SQL, (SCOPE, gen, incarnation)))
        self.assertIn('USING PRIMARY KEY', plan)
        self.assertNotIn('SCAN', plan)
        self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(store.db.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(store.db.execute('PRAGMA fullfsync').fetchone()[0], 1)

        self.assertEqual(self.raw_rows(store.path),
                         [(SCOPE, gen, incarnation, digest, canon(binding))])
        self.assertEqual(store.verify(), {'store_id': store.store_id, 'records': 1,
                                          'generations': (gen,)})
        with self.pinned() as token:
            self.assertEqual(load_pinned_binding(self.index, token, store), binding)
            rows = search_pinned_candidates(self.index, token, store, 'topic', 5)
        self.assertEqual([row['path'] for row in rows], ['plans/alpha.md'])
        self.assertEqual(rows[0]['generation'], gen)

    def test_duplicate_is_idempotent_and_rows_are_insert_only(self):
        _catalog, binding, store = self.basic()
        before = self.raw_rows(store.path)
        self.assertEqual(persist_generation_binding(store, self.index, binding)['status'],
                         'exists')
        again = GenerationSourceBinding.from_record(json.loads(canon(binding)))
        self.assertEqual(store.put(again)['status'], 'exists')
        self.assertEqual(self.raw_rows(store.path), before)

        with contextlib.closing(sqlite3.connect(store.path, isolation_level=None)) as db:
            for sql in ('UPDATE bindings SET record=record', 'DELETE FROM bindings',
                        'INSERT OR REPLACE INTO bindings SELECT * FROM bindings'):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'binding_immutable'):
                    db.execute(sql)
            for sql in ('UPDATE meta SET value=value', 'DELETE FROM meta',
                        "INSERT INTO meta VALUES('extra', 'x')"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'binding_store_sealed'):
                    db.execute(sql)
            # The digest column must equal the generation-name suffix.
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'CHECK constraint failed'):
                db.execute('INSERT INTO bindings VALUES(?,?,?,?,?)',
                           (SCOPE, 'g-' + '0' * 32 + '-b' + '1' * 64,
                            binding.db_incarnation, '2' * 64, '{}'))
        self.assertEqual(self.raw_rows(store.path), before)
        self.assertEqual(store.get(SCOPE, binding.generation, binding.db_incarnation), binding)

    def test_restart_reopens_durable_bindings(self):
        _catalog, binding, store = self.basic()
        store_id, path = store.store_id, store.path
        store.close()
        self.index.close()
        index = LayaIndexA(self.state / 'index.sqlite')
        self.addCleanup(index.close)
        store = GenerationBindingStore(path, scope=SCOPE, db_incarnation=index.incarnation(),
                                       roots=self.roots)
        self.addCleanup(store.close)
        self.assertEqual(store.store_id, store_id)
        with self.pinned(index) as token:
            self.assertEqual(load_pinned_binding(index, token, store), binding)
            rows = search_pinned_candidates(index, token, store, 'topic', 5)
        self.assertEqual(rows[0]['body'], '# Alpha\nalpha topic body\n')

        # Creation never clobbers, refuses a leftover journal, and leaves no temp.
        with self.assertRaisesRegex(IndexContractError, 'binding_store_exists'):
            GenerationBindingStore(path, scope=SCOPE, db_incarnation=index.incarnation(),
                                   roots=self.roots, create=True)
        fresh = self.state / 'fresh.sqlite'
        Path(str(fresh) + '-journal').write_bytes(b'')
        with self.assertRaisesRegex(IndexContractError, 'stale_binding_store_sidecar'):
            GenerationBindingStore(fresh, scope=SCOPE, db_incarnation=index.incarnation(),
                                   roots=self.roots, create=True)
        self.assertFalse(fresh.exists())
        self.assertEqual(self.reopen(path, db_incarnation=index.incarnation()).store_id,
                         store_id)
        self.assertEqual([p.name for p in self.state.iterdir()
                          if p.name.startswith(('.bindings', '.fresh'))
                          or p.name == 'bindings.sqlite-journal'], [])

    def test_record_identity_is_strict(self):
        _catalog, binding, store = self.basic()
        before = store.verify()
        renamed = tuple((name, root + '-b', dev, ino) for name, root, dev, ino in binding.roots)
        for bad in (dataclasses.replace(binding, scope='other-scope'),
                    dataclasses.replace(binding, db_incarnation='f' * 32),
                    dataclasses.replace(binding, roots=binding.roots[:1]),
                    dataclasses.replace(binding, roots=renamed)):
            bad = cap.bind_generation(bad)  # correctly named: identity is what refuses
            self.assert_stale(lambda bad=bad: store.put(bad),
                              'does not match binding store')
        with self.assertRaises(ValueError):
            store.put(binding.as_record())
        self.assertEqual(store.verify(), before)
        # Same collection paths with a new root node (a recreated root directory)
        # are a legitimate new generation under a new name; the query re-checks
        # the live node.
        recreated = cap.bind_generation(with_root(binding, ino=binding.roots[0][3] + 1))
        self.assertNotEqual(recreated.generation[-64:], binding.generation[-64:])
        self.assertEqual(store.put(recreated)['status'], 'stored')

    def test_store_identity_mismatch_fails_closed(self):
        _catalog, _binding, store = self.basic()
        path = store.path
        self.assert_stale(lambda: self.reopen(path, scope='other'),
                          'binding store scope differs')
        self.assert_stale(lambda: self.reopen(path, db_incarnation='f' * 32),
                          'binding store incarnation differs')
        self.assert_stale(lambda: self.reopen(path, roots={'plans': str(self.plans)}),
                          'binding store roots differ')
        self.assert_stale(lambda: self.reopen(self.state / 'absent.sqlite'),
                          'binding store missing')
        foreign = self.state / 'foreign.sqlite'
        self.raw(foreign, 'CREATE TABLE t(x)')
        self.assert_stale(lambda: self.reopen(foreign), 'binding store format differs')
        text = self.state / 'text.sqlite'
        text.write_text('not a database\n' * 20)
        self.assert_stale(lambda: self.reopen(text), 'binding store unreadable')


class NamingTests(CapacityCase):
    def test_generation_name_helpers(self):
        catalog, binding, _store = self.basic()
        nonce = 'a' * 32
        digest = cap.binding_digest(binding)
        name = cap.bound_generation_name(binding, nonce)
        self.assertEqual(name, f'g-{nonce}-b{digest}')
        self.assertEqual(len(name), 100)
        self.assertEqual(cap.generation_digest(name), digest)
        renamed = cap.bind_generation(binding, nonce)
        self.assertEqual(renamed.generation, name)
        self.assertEqual(dataclasses.replace(renamed, generation=binding.generation), binding)
        # The digest excludes the self-referential name.
        self.assertEqual(cap.binding_digest(renamed), digest)
        self.assertEqual(cap.require_bound_generation(renamed), digest)
        for bad_nonce in ('A' * 32, 'a' * 31, 'a' * 33, '', 'g' * 32, 7):
            with self.assertRaises(ValueError):
                cap.bound_generation_name(binding, bad_nonce)
        with self.assertRaises(ValueError):
            self.capture(catalog, nonce='x')
        for unbound in ('g1', 'g-' + nonce, name.upper(), name[:-1], name + '0',
                        name + '\n', ' ' + name, name.replace('-b', '-c', 1), None, 3):
            self.assertIsNone(cap.generation_digest(unbound))

        # Every committed field moves the digest, including root node identity.
        variants = (dataclasses.replace(binding, scope='other-scope'),
                    dataclasses.replace(binding, source_manifest='e' * 64),
                    dataclasses.replace(binding, catalog_scan_id='f' * 32),
                    dataclasses.replace(binding, catalog_raw_digest='d' * 64),
                    dataclasses.replace(binding, db_incarnation='f' * 32),
                    with_root(binding, root=binding.roots[0][1] + '-x'),
                    with_root(binding, dev=binding.roots[0][2] + 1),
                    with_root(binding, ino=binding.roots[0][3] + 1))
        digests = {cap.binding_digest(item) for item in variants} | {digest}
        self.assertEqual(len(digests), len(variants) + 1)

    def test_put_rejects_unbound_or_mismatched_names(self):
        _catalog, binding, store = self.basic()
        before = store.verify()
        for name in ('g1', 'g-' + uuid.uuid4().hex, binding.generation.upper()):
            with self.assertRaisesRegex(IndexContractError, 'generation_name_unbound'):
                store.put(dataclasses.replace(binding, generation=name))
        # The existing bound name with any other field is another binding.
        for changed in (dataclasses.replace(binding, source_manifest='e' * 64),
                        dataclasses.replace(binding, catalog_scan_id='f' * 32),
                        with_root(binding, ino=binding.roots[0][3] + 1)):
            with self.assertRaisesRegex(IndexContractError, 'generation_name_mismatch'):
                persist_generation_binding(store, self.index, changed)
        self.assertEqual(store.verify(), before)

    def test_unsuffixed_head_fails_closed(self):
        catalog, g1, store = self.basic()
        self.write(self.plans, 'gamma.md', '# Gamma\ngamma topic body\n')
        self.rescan(catalog)
        legacy = self.capture_legacy(catalog, 'g-' + uuid.uuid4().hex)
        self.stage(catalog, legacy, g1.generation)
        self.publish(catalog, legacy, g1.generation)
        with self.pinned() as token:
            for call in (lambda: load_pinned_binding(self.index, token, store),
                         lambda: open_pinned_candidate_query(self.index, token, store),
                         lambda: search_pinned_candidates(self.index, token, store,
                                                          'topic', 5)):
                self.assert_stale(call, 'generation name is not bound')
        self.assert_stale(lambda: store.get(SCOPE, legacy.generation, legacy.db_incarnation),
                          'generation name is not bound')
        with self.assertRaisesRegex(IndexContractError, 'generation_name_unbound'):
            persist_generation_binding(store, self.index, legacy)
        self.assertEqual(store.verify()['generations'], (g1.generation,))

    def test_self_consistent_rewrite_cannot_rebind_generation(self):
        _catalog, binding, store = self.basic()
        gen, incarnation = binding.generation, binding.db_incarnation
        key = (SCOPE, gen, incarnation)
        where = ' WHERE scope=? AND generation=? AND db_incarnation=?'

        # (a) Same root paths, another root node (a swapped-in checkout). The
        # record is canonical, in identity and passes every row constraint.
        swapped = with_root(binding, ino=binding.roots[0][3] + 1)
        self.tamper(store.path, ('UPDATE bindings SET record=?' + where,
                                 (canon(swapped), *key)))
        self.assert_stale(lambda: store.get(*key),
                          'generation name does not commit to binding')
        with self.pinned() as token:
            self.assert_stale(lambda: search_pinned_candidates(self.index, token, store,
                                                               'topic', 5),
                              'generation name does not commit to binding')

        # (b) The digest column cannot be recomputed under the A-int name: the
        # CHECK refuses it, and dropping the CHECK changes the schema.
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'CHECK constraint failed'):
            self.tamper(store.path, ('UPDATE bindings SET binding_digest=?' + where,
                                     (cap.binding_digest(swapped), *key)))

        # (c) A full rewrite to another worktree with recomputed metadata, record,
        # digest and name is self-consistent and passes the audit, but it can
        # only live under a new name. The A-int head keeps its own name, so it
        # has no binding.
        roots_b = self.worktree_b()
        other = cap.bind_generation(dataclasses.replace(binding, roots=tuple(
            (name, roots_b[name], dev, ino) for name, _root, dev, ino in binding.roots)))
        roots_value = cap._canonical([[name, roots_b[name]]
                                      for name in sorted(roots_b)]).decode('ascii')
        self.tamper(store.path,
                    ('DELETE FROM bindings', ()),
                    ("UPDATE meta SET value=? WHERE key='roots'", (roots_value,)),
                    (cap._INSERT_SQL, (SCOPE, other.generation, incarnation,
                                       cap.binding_digest(other), canon(other))))
        store_b = self.reopen(store.path, roots=roots_b)
        self.assertEqual(store_b.verify()['generations'], (other.generation,))
        self.assertIsNone(store_b.get(*key))
        with self.pinned() as token:
            self.assert_stale(lambda: load_pinned_binding(self.index, token, store_b),
                              'generation source binding missing')
        self.assert_stale(lambda: store.get(*key), 'binding store roots differ')
        # Nor can the writer API attach the rewritten binding to the A-int name.
        with self.assertRaisesRegex(IndexContractError, 'generation_name_mismatch'):
            store_b.put(dataclasses.replace(other, generation=gen))


class CorruptionTests(CapacityCase):
    def test_row_corruption_fails_closed(self):
        _catalog, binding, store = self.basic()
        key = (SCOPE, binding.generation, binding.db_incarnation)
        update = ('UPDATE bindings SET record=? WHERE scope=? AND generation=? '
                  'AND db_incarnation=?')
        good = canon(binding)
        no_roots = {k: v for k, v in binding.as_record().items() if k != 'roots'}
        cases = (
            (good.replace('{"catalog_raw_digest":', '{ "catalog_raw_digest":', 1),
             'non-canonical binding record'),
            (good[:-1], 'unparseable binding record'),
            ('{"a":NaN}', 'unparseable binding record'),
            ('[]', 'non-canonical binding record'),
            (cap._canonical(no_roots).decode('ascii'), 'invalid binding record'),
            (canon(dataclasses.replace(binding,
                                       generation=cap.bound_generation_name(binding))),
             'record key differs'),
            (canon(dataclasses.replace(binding, scope='other-scope')),
             'record outside store identity'),
            (canon(dataclasses.replace(binding, catalog_scan_id='f' * 32)),
             'generation name does not commit to binding'),
        )
        for text, detail in cases:
            self.tamper(store.path, (update, (text, *key)))
            self.assert_stale(lambda: store.get(*key), detail)
            self.assert_stale(store.verify, detail)
            with self.pinned() as token:
                self.assert_stale(lambda: search_pinned_candidates(self.index, token, store,
                                                                   'topic', 5), detail)
            # A writer never re-confirms a corrupt row.
            self.assert_stale(lambda: store.put(binding), detail)
        self.tamper(store.path, (update, (good, *key)))
        self.assertEqual(store.get(*key), binding)

    def test_file_and_schema_damage_fails_closed(self):
        _catalog, binding, store = self.basic()
        key = (SCOPE, binding.generation, binding.db_incarnation)

        def clone(name):
            path = self.state / name
            shutil.copyfile(store.path, path)
            return path

        path = clone('no-trigger.sqlite')
        self.raw(path, 'DROP TRIGGER binding_update_seal')
        self.assert_stale(lambda: self.reopen(path), 'binding store schema differs')
        path = clone('app-id.sqlite')
        self.raw(path, 'PRAGMA application_id=1')
        self.assert_stale(lambda: self.reopen(path), 'binding store format differs')
        path = clone('wal.sqlite')
        self.raw(path, 'PRAGMA journal_mode=WAL')
        self.assert_stale(lambda: self.reopen(path), 'binding store journal mode differs')
        path = clone('header.sqlite')
        with open(path, 'r+b') as handle:
            handle.write(b'\0' * 16)
        self.assert_stale(lambda: self.reopen(path), 'binding store unreadable')
        path = clone('scope.sqlite')
        self.tamper(path, ("UPDATE meta SET value='other' WHERE key='scope'", ()))
        self.assert_stale(lambda: self.reopen(path), 'binding store scope differs')
        path = clone('extra-meta.sqlite')
        self.tamper(path, ("INSERT INTO meta VALUES('extra', 'x')", ()))
        self.assert_stale(lambda: self.reopen(path),
                          'binding store corrupt: invalid store metadata')
        # A store id rewritten under an open instance is another store.
        path = clone('store-id.sqlite')
        opened = self.reopen(path)
        self.tamper(path, ("UPDATE meta SET value=? WHERE key='store_id'", ('e' * 32,)))
        self.assert_stale(lambda: opened.get(*key), 'binding store replaced')
        self.assertEqual(store.get(*key), binding)

    def test_replaced_or_symlinked_store_fails_closed(self):
        _catalog, binding, store = self.basic()
        key = (SCOPE, binding.generation, binding.db_incarnation)
        path = store.path
        copy = self.state / 'copy.sqlite'
        shutil.copyfile(path, copy)
        os.replace(copy, path)
        self.assert_stale(lambda: store.get(*key), 'binding store replaced')
        link = self.state / 'link.sqlite'
        link.symlink_to(path)
        with self.assertRaisesRegex(IndexContractError, 'invalid_explicit_path'):
            self.reopen(link)
        fresh = self.reopen(path)
        self.assertEqual(fresh.get(*key), binding)
        # A second hard link (e.g. a temp name left by a crash) is refused.
        alias = self.state / 'alias.sqlite'
        os.link(path, alias)
        self.assert_stale(lambda: fresh.get(*key), 'binding store has multiple links')
        self.assert_stale(lambda: self.reopen(path), 'binding store has multiple links')
        alias.unlink()
        self.assertEqual(fresh.get(*key), binding)
        target = self.state / 'target.sqlite'
        os.replace(path, target)
        path.symlink_to(target)
        self.assert_stale(lambda: fresh.get(*key), 'binding store replaced')
        path.unlink()
        self.assert_stale(lambda: fresh.get(*key), 'binding store missing')


class RecoveryTests(CapacityCase):
    def staged_g2(self, catalog, g1):
        self.write(self.plans, 'gamma.md', '# Gamma\ngamma topic body\n')
        self.rescan(catalog)
        g2 = self.capture(catalog)
        self.stage(catalog, g2, g1.generation)
        return g2

    def test_rollback_before_commit_stores_nothing(self):
        catalog, g1, store = self.basic()
        g2 = self.staged_g2(catalog, g1)
        with self.assertRaises(InjectedFault):
            persist_generation_binding(store, self.index, g2, fault_at='before_commit')
        self.assertIsNone(store.get(SCOPE, g2.generation, g2.db_incarnation))
        self.assertEqual(store.verify()['generations'], (g1.generation,))
        self.assertFalse(Path(str(store.path) + '-journal').exists())
        self.assertEqual(persist_generation_binding(store, self.index, g2)['status'], 'stored')

    def test_unacknowledged_commit_is_harmless_orphan(self):
        catalog, g1, store = self.basic()
        g2 = self.staged_g2(catalog, g1)
        with self.assertRaises(InjectedFault):
            persist_generation_binding(store, self.index, g2, fault_at='after_commit')
        # Durable but never acknowledged: nothing was published, g1 still serves.
        self.assertEqual(store.verify()['generations'],
                         tuple(sorted((g1.generation, g2.generation))))
        with self.pinned() as token:
            self.assertEqual(load_pinned_binding(self.index, token, store), g1)
            self.assertEqual(search_pinned_candidates(self.index, token, store,
                                                      'gamma', 5), [])
        # The retry is idempotent; then publish proceeds.
        self.assertEqual(persist_generation_binding(store, self.index, g2)['status'],
                         'exists')
        self.publish(catalog, g2, g1.generation)
        with self.pinned() as token:
            rows = search_pinned_candidates(self.index, token, store, 'gamma', 5)
        self.assertEqual([row['path'] for row in rows], ['plans/gamma.md'])

    def test_process_death_inside_put(self):
        catalog, g1, store = self.basic()
        g2 = self.staged_g2(catalog, g1)
        args = (CORE, store.path, SCOPE, g2.db_incarnation, json.dumps(self.roots),
                canon(g2))
        died = self.run_child(PUT_CHILD, *args, 'before_commit')
        self.assertEqual(died.returncode, 70, died.stderr)
        restarted = self.reopen(store.path)
        self.assertIsNone(restarted.get(SCOPE, g2.generation, g2.db_incarnation))
        self.assertEqual(restarted.verify()['generations'], (g1.generation,))

        died = self.run_child(PUT_CHILD, *args, 'after_commit')
        self.assertEqual(died.returncode, 70, died.stderr)
        restarted = self.reopen(store.path)
        self.assertEqual(restarted.get(SCOPE, g2.generation, g2.db_incarnation), g2)
        self.assertEqual(persist_generation_binding(restarted, self.index, g2)['status'],
                         'exists')
        self.assertFalse(Path(str(store.path) + '-journal').exists())

    def test_hot_journal_is_rolled_back_on_restart(self):
        catalog, g1, store = self.basic()
        journal = Path(str(store.path) + '-journal')
        died = self.run_child(HOT_JOURNAL_CHILD, store.path, SCOPE, g1.db_incarnation)
        self.assertEqual(died.returncode, 70, died.stderr)
        self.assertTrue(journal.exists())  # uncommitted pages reached the DB file
        expected = {'store_id': store.store_id, 'records': 1,
                    'generations': (g1.generation,)}
        # verify() reads every row: none of the child's 400 uncommitted inserts
        # survive. A cold (non-hot) journal file may legally remain on disk, so
        # the invariant is the committed state, not the journal's absence.
        restarted = self.reopen(store.path)
        self.assertEqual(restarted.verify(), expected)
        self.assertEqual(store.get(SCOPE, g1.generation, g1.db_incarnation), g1)
        # A second restart sees the same state: any leftover journal is inert.
        self.assertEqual(self.reopen(store.path).verify(), expected)

        # The store stays writable: a later valid append commits and survives restart.
        g2 = self.staged_g2(catalog, g1)
        self.assertEqual(persist_generation_binding(restarted, self.index, g2)['status'],
                         'stored')
        again = self.reopen(store.path)
        self.assertEqual(again.get(SCOPE, g2.generation, g2.db_incarnation), g2)
        self.assertEqual(again.verify(), {'store_id': store.store_id, 'records': 2,
                                          'generations': tuple(sorted((g1.generation,
                                                                       g2.generation)))})


class IncarnationTests(CapacityCase):
    def test_recreated_database_rejects_old_store(self):
        catalog, old, store = self.basic()
        db = self.state / 'index.sqlite'
        self.index.close()
        for suffix in ('', '-wal', '-shm'):
            Path(str(db) + suffix).unlink(missing_ok=True)
        self.index = LayaIndexA(db, create=True)
        self.addCleanup(self.index.close)
        self.assertNotEqual(self.index.incarnation(), old.db_incarnation)

        self.assert_stale(lambda: self.reopen(store.path), 'binding store incarnation differs')
        # Same nonce, new incarnation: the name differs because it commits to it.
        fresh = self.capture(catalog, nonce=old.generation[2:34])
        self.assertNotEqual(fresh.generation, old.generation)
        self.assert_stale(lambda: persist_generation_binding(store, self.index, fresh),
                          'database incarnation differs from binding store')
        self.assert_stale(lambda: store.put(fresh), 'does not match binding store')

        store2 = open_binding_store(self.state / 'bindings-2.sqlite', self.index, catalog,
                                    create=True)
        self.addCleanup(store2.close)
        self.stage(catalog, fresh, None)
        self.assertEqual(persist_generation_binding(store2, self.index, fresh)['status'],
                         'stored')
        self.publish(catalog, fresh, None)
        with self.pinned() as token:
            self.assert_stale(lambda: load_pinned_binding(self.index, token, store),
                              'database incarnation differs from binding store')
            # Seal-matching scope, name and manifest, old incarnation: reaches
            # CandidateQuery's incarnation check, not its seal check.
            stale = dataclasses.replace(fresh, db_incarnation=old.db_incarnation)
            self.assertEqual(stale.source_manifest, old.source_manifest)
            self.assert_stale(lambda: CandidateQuery(self.index, token, stale),
                              'database incarnation differs from binding')
            self.assertEqual(load_pinned_binding(self.index, token, store2), fresh)
        self.assertEqual(store.verify()['generations'], (old.generation,))


class BranchIdentityTests(CapacityCase):
    def test_other_worktree_roots_are_not_interchangeable(self):
        _catalog, binding, store = self.basic()
        roots_b = self.worktree_b()
        catalog_b = self.scanned_catalog(roots_b, 'catalog-b.sqlite')
        binding_b = self.capture(catalog_b)
        # Byte-identical worktree, same manifest, different roots and name digest.
        self.assertEqual(binding_b.source_manifest, binding.source_manifest)
        self.assertNotEqual(cap.binding_digest(binding_b), cap.binding_digest(binding))
        self.assert_stale(lambda: store.put(binding_b), 'does not match binding store')
        self.assert_stale(lambda: self.reopen(store.path, roots=roots_b),
                          'binding store roots differ')
        self.assert_stale(lambda: open_binding_store(store.path, self.index, catalog_b),
                          'binding store roots differ')
        self.assertEqual(store.verify()['generations'], (binding.generation,))

        # Another checkout swapped in at the same root path: the stored binding
        # loads, but the query refuses the replaced root node.
        moved = self.plans.with_name('plans-orig')
        self.plans.rename(moved)
        shutil.copytree(moved, self.plans)
        with self.pinned() as token:
            self.assertEqual(load_pinned_binding(self.index, token, store), binding)
            self.assert_stale(lambda: open_pinned_candidate_query(self.index, token, store),
                              'collection root replaced: plans')
            self.assert_stale(lambda: search_pinned_candidates(self.index, token, store,
                                                               'topic', 5),
                              'collection root replaced: plans')


class CurrentCandidateTests(CapacityCase):
    def test_pinned_generation_uses_its_own_binding(self):
        catalog, g1, store = self.basic()
        token1, _gen = self.index.pin(SCOPE)
        self.addCleanup(self.index.release, token1)
        self.write(self.plans, 'gamma.md', '# Gamma\ngamma topic body\n')
        self.rescan(catalog)
        g2 = self.publish_persisted(catalog, store, g1.generation)
        self.assertNotEqual(g2.catalog_scan_id, g1.catalog_scan_id)

        # The old pin keeps its own binding; it is never the current head's.
        self.assertEqual(load_pinned_binding(self.index, token1, store), g1)
        self.assertEqual(search_pinned_candidates(self.index, token1, store, 'gamma', 5), [])
        rows = search_pinned_candidates(self.index, token1, store, 'topic', 5)
        self.assertEqual([row['path'] for row in rows], ['plans/alpha.md'])
        self.assertEqual(rows[0]['snapshot'], g1.catalog_scan_id)
        with self.pinned() as token2:
            self.assertEqual(load_pinned_binding(self.index, token2, store), g2)
            rows = search_pinned_candidates(self.index, token2, store, 'gamma', 5)
        self.assertEqual([row['path'] for row in rows], ['plans/gamma.md'])
        self.assertEqual(rows[0]['snapshot'], g2.catalog_scan_id)

    def test_published_head_without_binding_fails_closed(self):
        catalog, g1, store = self.basic()
        self.write(self.plans, 'gamma.md', '# Gamma\ngamma topic body\n')
        self.rescan(catalog)
        g2 = self.capture(catalog)
        self.stage(catalog, g2, g1.generation)
        self.publish(catalog, g2, g1.generation)  # contract violation: never persisted
        with self.pinned() as token:
            for call in (lambda: load_pinned_binding(self.index, token, store),
                         lambda: open_pinned_candidate_query(self.index, token, store),
                         lambda: search_pinned_candidates(self.index, token, store,
                                                          'topic', 5)):
                self.assert_stale(call, 'generation source binding missing')
        # A binding is never rebuilt from a later snapshot to cover the head.
        self.assertEqual(store.verify()['generations'], (g1.generation,))

    def test_rewritten_candidate_is_dropped_through_store(self):
        _catalog, binding, store = self.basic()
        self.alpha.write_bytes(b'# Alpha\nalpha topic BODY\n')
        with self.pinned() as token:
            with self.assertRaises(CatalogPending) as caught:
                search_pinned_candidates(self.index, token, store, 'topic', 5)
            self.assertEqual(caught.exception.detail['dropped'], {'hash_mismatch': 1})
            self.assertEqual(caught.exception.detail['generation'], binding.generation)
            with open_pinned_candidate_query(self.index, token, store) as query:
                rows = query.search('beta', 5)
                stats = query.finish()
        self.assertEqual([row['path'] for row in rows], ['plans/beta.md'])
        self.assertEqual(rows[0]['body'], '# Beta\nbeta body\n')
        self.assertEqual(stats['files_opened'], 1)
        self.assertEqual(query.binding, binding)


class QueueAllocationTests(CapacityCase):
    def test_queue_allocates_the_bound_generation(self):
        self.write(self.plans, 'alpha.md', '# Alpha\nalpha topic body\n')
        catalog = self.scanned_catalog()
        store = open_binding_store(self.state / 'bindings.sqlite', self.index, catalog,
                                   create=True)
        self.addCleanup(store.close)
        queue = ClaimQueue.create(self.state / 'queue.sqlite',
                                  index_incarnation=self.index.incarnation())
        self.addCleanup(queue.close)

        legacy = self.capture_legacy(catalog, 'g-' + uuid.uuid4().hex)
        with self.assertRaisesRegex(IndexContractError, 'generation_name_unbound'):
            enqueue_bound_generation(queue, legacy, None)
        self.assertEqual(queue.active_for_scope(SCOPE), [])

        binding = self.capture(catalog)
        job = enqueue_bound_generation(queue, binding, None)
        self.assertEqual((job['scope'], job['gen'], job['source_manifest'], job['base_gen']),
                         (SCOPE, binding.generation, binding.source_manifest, None))
        self.assertEqual([item['gen'] for item in queue.active_for_scope(SCOPE)],
                         [binding.generation])
        self.assertEqual(stage_from_catalog(self.index, catalog, job['gen'], base=None,
                                            claim_digest=job['claim_digest'],
                                            claim_epoch=job['claim_epoch'],
                                            binding=binding), 'prepared')
        self.assertEqual(persist_generation_binding(store, self.index, binding)['status'],
                         'stored')
        self.assertEqual(publish_from_catalog(self.index, catalog, None, job['gen'],
                                              job['claim'], claim_digest=job['claim_digest'],
                                              claim_epoch=job['claim_epoch'],
                                              binding=binding), 'published')
        with self.pinned() as token:
            self.assertEqual(load_pinned_binding(self.index, token, store), binding)
            rows = search_pinned_candidates(self.index, token, store, 'topic', 5)
        self.assertEqual([row['path'] for row in rows], ['plans/alpha.md'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
