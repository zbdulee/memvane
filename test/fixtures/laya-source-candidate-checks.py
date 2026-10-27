"""Disposable returned-candidate verification checks.

Synthetic temp-dir corpora and a temp A-int DB only; no operating DB or source.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_source_catalog as catalog_mod
from laya_index_a import IndexContractError, LayaIndexA
from laya_index_source_port import scan_collections
from laya_source_catalog import (CandidateQuery, CatalogPending,
                                 GenerationSourceBinding, LayaSourceCatalog,
                                 capture_generation_binding, publish_from_catalog,
                                 search_candidates_verified, stage_from_catalog)

SCOPE = 'scope-candidate'
CLAIM = 'c' * 64
FILLER = 'lorem ipsum dolor sit amet '


def drive(catalog, **budget):
    for _ in range(10_000):
        result = catalog.step(**budget)
        if result['status'] != 'pending':
            return result
    raise AssertionError('scan did not settle')


def sized(head, size):
    return (head + FILLER * (size // len(FILLER) + 1))[:size]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def candidate_row(path, hash_, generation='g1'):
    """A row a defective fetch could return; its body is never served."""
    return {'scope': SCOPE, 'generation': generation, 'path': path, 'hash': hash_,
            'score': 0.0, 'body': 'never served'}


class CandidateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-source-candidate-')
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

    def scanned_catalog(self):
        catalog = LayaSourceCatalog(self.state / 'catalog.sqlite', self.roots, SCOPE,
                                    create=True)
        self.addCleanup(catalog.close)
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog)['status'], 'complete')
        return catalog

    def rescan(self, catalog):
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog)['status'], 'complete')

    def publish_bound(self, catalog, gen, base):
        snapshot = catalog.require_complete()
        binding = capture_generation_binding(self.index, catalog, snapshot, gen)
        self.assertEqual(stage_from_catalog(self.index, catalog, gen, base=base,
                                            claim_digest=CLAIM, claim_epoch=0,
                                            binding=binding), 'prepared')
        self.assertEqual(publish_from_catalog(self.index, catalog, base, gen,
                                              'claim-' + gen, claim_digest=CLAIM,
                                              claim_epoch=0, binding=binding),
                         'published')
        return binding

    def basic(self):
        """alpha/beta under plans, one wiki card; generation g1 bound and published."""
        self.alpha = self.write(self.plans, 'alpha.md', '# Alpha\nalpha topic body\n')
        self.beta = self.write(self.plans, 'beta.md', '# Beta\nbeta body\n')
        self.card = self.write(self.wiki, 'card.md', '# Card\nalpha wiki card\n')
        catalog = self.scanned_catalog()
        return catalog, self.publish_bound(catalog, 'g1', None)

    @contextlib.contextmanager
    def pinned(self):
        token, _gen = self.index.pin(SCOPE)
        try:
            yield token
        finally:
            self.index.release(token)

    def query(self, binding, fts, limit=5, **kwargs):
        with self.pinned() as token:
            return search_candidates_verified(self.index, token, binding, fts, limit,
                                              **kwargs)

    def ranked(self, fts, limit):
        with self.pinned() as token:
            return [row['path'] for row in self.index.search(token, fts, limit)]

    def assert_stale(self, call, detail):
        with self.assertRaisesRegex(IndexContractError, 'index_stale') as caught:
            call()
        self.assertNotIsInstance(caught.exception, CatalogPending)
        self.assertIn(detail, str(caught.exception.__cause__))

    def assert_pending(self, call):
        with self.assertRaises(CatalogPending) as caught:
            call()
        self.assertEqual(str(caught.exception), 'index_stale')
        self.assertEqual(caught.exception.reason, 'candidates_unverified')
        return caught.exception.detail

    @contextlib.contextmanager
    def counted_fetches(self):
        sizes = []
        original = self.index.search

        def counting(token, fts, limit, **kwargs):
            sizes.append(limit)
            return original(token, fts, limit, **kwargs)
        self.index.search = counting
        try:
            yield sizes
        finally:
            del self.index.search

    @contextlib.contextmanager
    def no_enumeration(self):
        """Record any fd-based directory listing; forbid the port's full scan."""
        calls = []
        real_listdir, real_scandir = os.listdir, os.scandir
        real_scan = catalog_mod.scan_collections

        def listdir(path='.'):
            if isinstance(path, int):
                calls.append(('listdir', path))
            return real_listdir(path)

        def scandir(path='.'):
            if isinstance(path, int):
                calls.append(('scandir', path))
            return real_scandir(path)

        def forbidden(*_args, **_kwargs):
            raise AssertionError('whole-tree scan')
        os.listdir, os.scandir = listdir, scandir
        catalog_mod.scan_collections = forbidden
        try:
            yield calls
        finally:
            os.listdir, os.scandir = real_listdir, real_scandir
            catalog_mod.scan_collections = real_scan

    def swap_in_copy(self, root):
        """Replace ``root`` by a byte-identical copy (a different worktree inode)."""
        copy = root.with_name(root.name + '-copy')
        moved = root.with_name(root.name + '-orig')
        shutil.copytree(root, copy)
        root.rename(moved)
        copy.rename(root)

        def restore():
            root.rename(copy)
            moved.rename(root)
            shutil.rmtree(copy)
        return restore


class FreshnessTests(CandidateCase):
    def test_same_size_mtime_rewrite_is_dropped_by_hash(self):
        _catalog, binding = self.basic()
        rows = self.query(binding, 'topic')
        self.assertEqual([row['path'] for row in rows], ['plans/alpha.md'])
        self.assertEqual(rows[0]['body'], '# Alpha\nalpha topic body\n')
        self.assertEqual(rows[0]['snapshot'], binding.catalog_scan_id)

        before = os.stat(self.alpha)
        with open(self.alpha, 'r+b') as handle:
            handle.write(b'# Alpha\nalpha topic BODY\n')
        os.utime(self.alpha, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = os.stat(self.alpha)
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (before.st_ino, before.st_size, before.st_mtime_ns))
        # Stat signatures carry no acceptance weight: make them all identical.
        original = catalog_mod._sig_text
        catalog_mod._sig_text = lambda _st: 'constant'
        self.addCleanup(setattr, catalog_mod, '_sig_text', original)

        detail = self.assert_pending(lambda: self.query(binding, 'topic'))
        self.assertEqual(detail, {
            'reason': 'candidates_unverified', 'scope': SCOPE, 'generation': 'g1',
            'snapshot': binding.catalog_scan_id, 'limit': 5, 'rounds': 1,
            'fetched': [5], 'candidates': 1, 'dropped': {'hash_mismatch': 1}})
        # The indexed bytes are served again; acceptance is content only.
        self.alpha.write_bytes(b'# Alpha\nalpha topic body\n')
        self.assertEqual(len(self.query(binding, 'topic')), 1)

    def test_deleted_candidate_is_dropped_not_deleted(self):
        catalog, binding = self.basic()
        snapshot = catalog.require_servable()
        self.alpha.unlink()
        detail = self.assert_pending(lambda: self.query(binding, 'topic'))
        self.assertEqual(detail['dropped'], {'missing': 1})
        # A dropped candidate is not deletion evidence: the snapshot is intact.
        status = catalog.status()
        self.assertTrue(status['servable'])
        self.assertEqual(status['current']['files'], 3)
        self.assertIsNotNone(catalog.lookup(snapshot, 'plans/alpha.md'))
        # Other candidates of the same query are still served.
        rows = self.query(binding, 'alpha')
        self.assertEqual([row['path'] for row in rows], ['wiki/card.md'])

    def test_new_file_lags_without_enumeration(self):
        _catalog, binding = self.basic()
        self.write(self.plans, 'gamma.md', '# Gamma\ngammaword arrives late\n')
        self.beta.write_text('# Beta\nbeta rewritten with deltaword\n', encoding='utf-8')
        with self.no_enumeration() as calls:
            # Index lag: new and updated text is undiscoverable until sync.
            self.assertEqual(self.query(binding, 'gammaword'), [])
            self.assertEqual(self.query(binding, 'deltaword'), [])
            # Stale exposure is not: the old indexed term finds beta, and it is dropped.
            detail = self.assert_pending(lambda: self.query(binding, 'beta'))
            self.assertEqual(detail['dropped'], {'hash_mismatch': 1})
            self.assertEqual(len(self.query(binding, 'topic')), 1)
        self.assertEqual(calls, [])


class BindingTests(CandidateCase):
    def test_root_identity_and_worktree_swap(self):
        _catalog, binding = self.basic()
        self.assertEqual(len(self.query(binding, 'topic')), 1)

        restore = self.swap_in_copy(self.plans)  # same path and bytes, new inode
        self.assert_stale(lambda: self.query(binding, 'topic'),
                          'collection root replaced: plans')
        restore()
        self.assertEqual(len(self.query(binding, 'topic')), 1)

        # A binding naming another worktree's path with this root's inode.
        other = self.plans.with_name('plans-worktree')
        shutil.copytree(self.plans, other)
        roots = tuple((name, str(other) if name == 'plans' else root, dev, ino)
                      for name, root, dev, ino in binding.roots)
        foreign = dataclasses.replace(binding, roots=tuple(sorted(roots)))
        self.assert_stale(lambda: self.query(foreign, 'topic'),
                          'collection root replaced: plans')

        moved = self.plans.with_name('plans-away')
        self.plans.rename(moved)
        self.assert_stale(lambda: self.query(binding, 'topic'),
                          'collection root unavailable: plans')
        moved.rename(self.plans)

        # A swap after rows were verified still fails the whole query at finish.
        with self.pinned() as token:
            query = CandidateQuery(self.index, token, binding)
            self.addCleanup(query.close)
            self.assertEqual(len(query.search('topic', 5)), 1)
            restore = self.swap_in_copy(self.wiki)
            try:
                self.assert_stale(query.finish, 'collection root replaced: wiki')
            finally:
                restore()
        self.assertEqual(len(self.query(binding, 'topic')), 1)

    def test_binding_seal_and_incarnation_gate(self):
        catalog, b1 = self.basic()
        self.assert_stale(lambda: self.query(None, 'topic'),
                          'generation source binding missing')
        for wrong in (dataclasses.replace(b1, generation='g0'),
                      dataclasses.replace(b1, scope='other-scope'),
                      dataclasses.replace(b1, source_manifest='0' * 64)):
            self.assert_stale(lambda: self.query(wrong, 'topic'),
                              'view seal does not match generation source binding')
        other = LayaIndexA(self.state / 'other.sqlite', create=True)
        self.addCleanup(other.close)
        self.assertNotEqual(other.incarnation(), b1.db_incarnation)
        foreign = dataclasses.replace(b1, db_incarnation=other.incarnation())
        self.assert_stale(lambda: self.query(foreign, 'topic'),
                          'database incarnation differs from binding')

        # Capture needs the COMPLETE current snapshot. A pending newer scan does
        # not starve it; a newer scan that publishes replaces it.
        stale_snapshot = catalog.require_complete()
        self.alpha.write_text('# Alpha\nalpha topic second body\n', encoding='utf-8')
        catalog.request_scan()
        capture_generation_binding(self.index, catalog, stale_snapshot, 'g2')
        self.assertEqual(drive(catalog)['status'], 'complete')
        self.assert_stale(
            lambda: capture_generation_binding(self.index, catalog, stale_snapshot, 'g2'),
            'catalog snapshot changed')
        snapshot = catalog.require_servable()
        b2 = capture_generation_binding(self.index, catalog, snapshot, 'g2')
        self.assertNotEqual(b2.catalog_scan_id, b1.catalog_scan_id)

        # Stage/publish refuse any binding that does not describe this state.
        for gen, wrong in (('g3', b2), ('g2', dataclasses.replace(b1, generation='g2'))):
            self.assert_stale(
                lambda: stage_from_catalog(self.index, catalog, gen, base='g1',
                                           claim_digest=CLAIM, claim_epoch=0,
                                           binding=wrong),
                'generation source binding does not match the catalog snapshot')
        self.assertEqual(stage_from_catalog(self.index, catalog, 'g2', base='g1',
                                            claim_digest=CLAIM, claim_epoch=0,
                                            binding=b2), 'prepared')
        recreated_db = dataclasses.replace(b2, db_incarnation=other.incarnation())
        self.assert_stale(
            lambda: publish_from_catalog(self.index, catalog, 'g1', 'g2', 'claim-g2',
                                         claim_digest=CLAIM, claim_epoch=0,
                                         binding=recreated_db),
            'generation source binding does not match the catalog snapshot')
        self.assertEqual(publish_from_catalog(self.index, catalog, 'g1', 'g2', 'claim-g2',
                                              claim_digest=CLAIM, claim_epoch=0,
                                              binding=b2), 'published')
        # The head moved: the old generation's binding no longer matches the pin.
        self.assert_stale(lambda: self.query(b1, 'topic'),
                          'view seal does not match generation source binding')
        rows = self.query(b2, 'second')
        self.assertEqual([row['body'] for row in rows],
                         ['# Alpha\nalpha topic second body\n'])

        # An exclusion committed mid-query changes the seal epoch: whole query fails.
        with self.pinned() as token:
            query = CandidateQuery(self.index, token, b2)
            self.addCleanup(query.close)
            self.assertEqual(len(query.search('second', 5)), 1)
            self.index.exclude(SCOPE, path='plans/beta.md')
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                query.finish()

    def test_binding_record_roundtrip_and_validation(self):
        _catalog, binding = self.basic()
        record = json.loads(json.dumps(binding.as_record()))
        self.assertEqual(GenerationSourceBinding.from_record(record), binding)
        self.assertEqual(binding.collections, self.roots)
        self.assertEqual([item[:2] for item in binding.roots],
                         [('plans', str(self.plans)), ('wiki', str(self.wiki))])
        self.assertEqual(binding.roots[0][2:],
                         (os.stat(self.plans).st_dev, os.stat(self.plans).st_ino))
        bad_records = (
            {**record, 'extra': 1},
            {**record, 'source_manifest': record['source_manifest'].upper()},
            {**record, 'db_incarnation': 'x' * 32},
            {**record, 'generation': ''},
            {**record, 'roots': list(reversed(record['roots']))},
            {**record, 'roots': [[record['roots'][0][0], record['roots'][0][1], -1, 1]]},
            {**record, 'roots': [[record['roots'][0][0], record['roots'][0][1] + '/', 1, 1]]},
            {**record, 'roots': [['.hidden', record['roots'][0][1], 1, 1]]},
            {**record, 'roots': [['a', '/x', 1, 1], ['b', '/x/y', 2, 2]]},
            {**record, 'roots': []},
            {**record, 1: 'x'},
            # Seven keys, one of them not a str: still ValueError, never TypeError.
            {**{k: v for k, v in record.items() if k != 'scope'}, 1: record['scope']},
            {**{k: v for k, v in record.items() if k != 'scope'}, b'scope': record['scope']},
        )
        for bad in bad_records:
            with self.assertRaises(ValueError):
                GenerationSourceBinding.from_record(bad)


class CandidateSafetyTests(CandidateCase):
    def test_unsafe_candidates_are_dropped(self):
        self.write(self.plans, 'a01.md', '# A1\nonlyone body\n')
        a02 = self.write(self.plans, 'a02.md', '# A2\nonlytwo body\n')
        self.write(self.plans, 'sub/a03.md', '# A3\nonlythree body\n')
        secret = self.write(self.plans, 'secret.md', '# Secret\nsecret body\n')
        hashed = self.write(self.plans, 'hashed.md', '# Hashed\nhashed body\n')
        card = self.write(self.wiki, 'card.md', '# Card\nwiki card\n')
        self.index.exclude(SCOPE, path='plans/secret.md')
        self.index.exclude(SCOPE, hash_=sha(hashed.read_bytes()))
        catalog = self.scanned_catalog()
        binding = self.publish_bound(catalog, 'g1', None)
        for word in ('onlyone', 'onlytwo', 'onlythree'):
            self.assertEqual(len(self.query(binding, word)), 1)

        # A symlinked file with identical bytes is not the indexed file.
        twin = self.base / 'outside-a02.md'
        twin.write_bytes(a02.read_bytes())
        a02.unlink()
        os.symlink(twin, a02)
        self.assertEqual(self.assert_pending(lambda: self.query(binding, 'onlytwo'))
                         ['dropped'], {'symlink': 1})
        # A symlinked directory component.
        real_sub = self.base / 'sub-real'
        (self.plans / 'sub').rename(real_sub)
        os.symlink(real_sub, self.plans / 'sub')
        self.assertEqual(self.assert_pending(lambda: self.query(binding, 'onlythree'))
                         ['dropped'], {'symlink': 1})
        # A device crossing (modelled: the module's no-follow lstat reports another dev).
        original = catalog_mod._lstat_at

        def other_device(dfd, name):
            st = original(dfd, name)
            if name != 'a01.md':
                return st
            values = list(st[:10])
            values[2] = st.st_dev + 1
            return os.stat_result(values)
        catalog_mod._lstat_at = other_device
        try:
            self.assertEqual(self.assert_pending(lambda: self.query(binding, 'onlyone'))
                             ['dropped'], {'crosses_device': 1})
        finally:
            catalog_mod._lstat_at = original
        self.assertEqual(len(self.query(binding, 'onlyone')), 1)

        # Rows a defective fetch could return; each is dropped, never read as served.
        self.write(self.plans, '.hidden.md', '# A1\nonlyone body\n')
        a01_hash = sha((self.plans / 'a01.md').read_bytes())
        card_hash = sha(card.read_bytes())

        row = candidate_row
        cases = (
            ([row('plans/.hidden.md', a01_hash)], None, 'unsafe_path'),
            ([row('plans/../wiki/card.md', card_hash)], None, 'unsafe_path'),
            ([row('plans/a01.py', a01_hash)], None, 'unsafe_path'),
            ([row('plans/secret.md', sha(secret.read_bytes()))], None, 'excluded'),
            ([row('plans/hashed.md', sha(hashed.read_bytes()))], None, 'excluded'),
        )
        with self.pinned() as token:
            query = CandidateQuery(self.index, token, binding)
            self.addCleanup(query.close)
            for rows, names, reason in cases:
                detail = self.assert_pending(
                    lambda: query.phase(lambda size: rows[:size], 5,
                                        collection_names=names))
                self.assertEqual(detail['dropped'], {reason: 1}, rows[0]['path'])
            self.assertEqual(query.stats['files_opened'], 0)  # rejected before any read
            mixed = query.phase(lambda size: [row('plans/.hidden.md', a01_hash),
                                              row('wiki/card.md', card_hash)][:size], 5)
            self.assertEqual([(r['path'], r['body']) for r in mixed],
                             [('wiki/card.md', '# Card\nwiki card\n')])
            self.assertEqual(query.stats['files_opened'], 1)
            self.assertEqual(query.stats['dropped'], {'unsafe_path': 4, 'excluded': 2})
            # A row from another generation is an A-int contract violation: the
            # whole query fails, including rows already verified by earlier phases.
            self.assert_stale(
                lambda: query.phase(lambda size: [dict(row('wiki/card.md', card_hash),
                                                       generation='g0')], 5),
                'candidate row outside the pinned generation')
            self.assert_stale(query.finish, 'candidate query already failed')

    def test_out_of_selection_row_poisons_query(self):
        _catalog, binding = self.basic()
        alpha_hash, card_hash = sha(self.alpha.read_bytes()), sha(self.card.read_bytes())
        # A fetch that ignores its collection scope violates the caller
        # contract: the query fails, it is not reported as candidates_unverified.
        for rows, names in (([candidate_row('wiki/card.md', card_hash)], ('plans',)),
                            ([candidate_row('other/alpha.md', alpha_hash)], None)):
            with self.pinned() as token:
                query = CandidateQuery(self.index, token, binding)
                self.addCleanup(query.close)
                self.assertEqual(len(query.search('topic', 5)), 1)
                self.assert_stale(
                    lambda: query.phase(lambda size: rows[:size], 5,
                                        collection_names=names),
                    'candidate row outside the selected collections')
                self.assertEqual(query.stats['files_opened'], 1)
                self.assertEqual(query.stats['dropped'], {})
                self.assert_stale(query.finish, 'candidate query already failed')


class IndexingTests(CandidateCase):
    """Bound stage/publish: scan-time indexing evidence, not live corpus freshness."""

    def test_pending_scan_does_not_starve_bound_indexing(self):
        catalog, _b1 = self.basic()
        s1 = catalog.require_servable()
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(catalog.step(max_files=1)['status'], 'pending')  # lists plans/
        self.assertEqual(catalog.status()['reason'], 'scan_pending')
        # The strict binding=None path still fails closed on a newer pending scan.
        self.assert_stale(catalog.require_servable, 'scan_pending')
        self.assert_stale(
            lambda: stage_from_catalog(self.index, catalog, 'g2', base='g1',
                                       claim_digest=CLAIM, claim_epoch=0),
            'scan_pending')
        # The bound path uses the current COMPLETE snapshot despite it.
        self.assertEqual(catalog.require_complete(), s1)
        b2 = self.publish_bound(catalog, 'g2', 'g1')
        self.assertEqual(b2.catalog_scan_id, s1.scan_id)
        self.assertEqual([row['path'] for row in self.query(b2, 'topic')],
                         ['plans/alpha.md'])

        # A newer scan that publishes replaces the snapshot: fail closed, then retry.
        b3 = capture_generation_binding(self.index, catalog, s1, 'g3')
        self.assertEqual(drive(catalog)['status'], 'complete')
        s2 = catalog.require_complete()
        self.assertNotEqual(s2.scan_id, s1.scan_id)
        self.assert_stale(lambda: capture_generation_binding(self.index, catalog, s1, 'g3'),
                          'catalog snapshot changed')
        mismatch = 'generation source binding does not match the catalog snapshot'
        self.assert_stale(lambda: stage_from_catalog(self.index, catalog, 'g3', base='g2',
                                                     claim_digest=CLAIM, claim_epoch=0,
                                                     binding=b3), mismatch)
        self.assert_stale(lambda: publish_from_catalog(self.index, catalog, 'g2', 'g3',
                                                       'claim-g3', claim_digest=CLAIM,
                                                       claim_epoch=0, binding=b3), mismatch)

        # Replacement mid-iteration: the dropped rows end the pages early and
        # the final check fails, so no partial generation is staged.
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(catalog.step(max_files=1)['status'], 'pending')
        b3 = capture_generation_binding(self.index, catalog, s2, 'g3')
        other = LayaSourceCatalog(self.state / 'catalog.sqlite', self.roots, SCOPE)
        self.addCleanup(other.close)
        real_read = catalog._read_member
        completed = []

        def publish_newer_scan_first(*args):
            if not completed:
                completed.append(drive(other)['status'])
            return real_read(*args)
        catalog._read_member = publish_newer_scan_first
        try:
            self.assert_stale(lambda: stage_from_catalog(self.index, catalog, 'g3', base='g2',
                                                         claim_digest=CLAIM, claim_epoch=0,
                                                         binding=b3),
                              'catalog snapshot changed')
        finally:
            del catalog._read_member
        self.assertEqual(completed, ['complete'])
        s3 = catalog.require_servable()
        self.assertNotEqual(s3.scan_id, s2.scan_id)
        b3 = self.publish_bound(catalog, 'g3', 'g2')  # retry from the new snapshot
        self.assertEqual(b3.catalog_scan_id, s3.scan_id)
        self.assertEqual(len(self.query(b3, 'topic')), 1)

        # Members are still rehashed against the bound snapshot while pending.
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(catalog.step(max_files=1)['status'], 'pending')
        b4 = capture_generation_binding(self.index, catalog, s3, 'g4')
        self.alpha.write_bytes(b'# Alpha\nalpha topic BODY\n')  # same size
        self.assert_stale(lambda: stage_from_catalog(self.index, catalog, 'g4', base='g3',
                                                     claim_digest=CLAIM, claim_epoch=0,
                                                     binding=b4),
                          'source bytes differ from catalog snapshot: plans/alpha.md')

    def test_bound_indexing_has_no_stat_hint_cap(self):
        catalog, _b1 = self.basic()
        snapshot = catalog.require_complete()
        # Synthetic snapshot metadata past the stat hint's default bound
        # (empty-directory rows); no large temp corpus is built.
        synthetic = catalog_mod.DEFAULT_VERIFY_STATS + 1
        catalog.db.execute('BEGIN IMMEDIATE')
        catalog.db.executemany(
            'INSERT INTO scan_dirs(seq,rel,collection,depth,node,sig,listed) '
            "VALUES(?,?,'plans',1,?,'synthetic',1)",
            ((snapshot.seq, f'plans/synthetic{i:05d}/', f'0:{i}')
             for i in range(synthetic)))
        catalog.db.commit()
        self.assertEqual(catalog.require_complete(), snapshot)
        self.assertGreater(catalog.status()['active']['backlog']['dirs_known'],
                           catalog_mod.DEFAULT_VERIFY_STATS)
        # binding=None keeps the strict stat hint and its bound.
        self.assert_stale(lambda: stage_from_catalog(self.index, catalog, 'g2', base='g1',
                                                     claim_digest=CLAIM, claim_epoch=0),
                          'freshness sweep exceeds max_verify_stats')

        # With a binding the hint is not run, so it is no corpus cap.
        original = catalog_mod.LayaSourceCatalog.verify_unchanged

        def forbidden(*_args, **_kwargs):
            raise AssertionError('stat hint used as a bound-indexing gate')
        catalog_mod.LayaSourceCatalog.verify_unchanged = forbidden
        self.addCleanup(setattr, catalog_mod.LayaSourceCatalog, 'verify_unchanged',
                        original)
        b2 = self.publish_bound(catalog, 'g2', 'g1')
        self.assertEqual(b2.catalog_scan_id, snapshot.scan_id)
        self.assertEqual([row['path'] for row in self.query(b2, 'topic')],
                         ['plans/alpha.md'])


class RefillTests(CandidateCase):
    def shared_corpus(self):
        for i in range(6):
            self.write(self.plans, f's{i}.md', f'# S{i}\nshared word {i}\n')
        self.write(self.wiki, 'card.md', '# Card\nwiki card\n')
        catalog = self.scanned_catalog()
        binding = self.publish_bound(catalog, 'g1', None)
        ranked = self.ranked('shared', 8)
        self.assertEqual(sorted(ranked), [f'plans/s{i}.md' for i in range(6)])
        return binding, ranked

    def rewrite(self, path):
        target = self.plans / path.split('/', 1)[1]
        target.write_text(target.read_text(encoding='utf-8') + 'changed\n',
                          encoding='utf-8')

    def test_mixed_candidates_refill_in_two_bounded_rounds(self):
        binding, ranked = self.shared_corpus()
        with self.counted_fetches() as sizes:
            rows = self.query(binding, 'shared', limit=2)
        self.assertEqual(sizes, [2])  # nothing dropped: one round
        self.assertEqual([row['path'] for row in rows], ranked[:2])

        self.rewrite(ranked[0])
        with self.counted_fetches() as sizes:
            rows = self.query(binding, 'shared', limit=2)
        self.assertEqual(sizes, [2, 8])
        self.assertEqual([row['path'] for row in rows], ranked[1:3])
        for row in rows:
            data = (self.plans / row['path'].split('/', 1)[1]).read_bytes()
            self.assertEqual((row['hash'], row['body']), (sha(data), data.decode('utf-8')))

        # No candidates at all is a normal empty answer after one round.
        with self.counted_fetches() as sizes:
            self.assertEqual(self.query(binding, 'nonexistentword'), [])
        self.assertEqual(sizes, [5])

    def test_all_invalid_candidates_are_pending_after_two_rounds(self):
        binding, ranked = self.shared_corpus()
        for path in ranked:
            self.rewrite(path)
        with self.counted_fetches() as sizes:
            detail = self.assert_pending(lambda: self.query(binding, 'shared', limit=2))
        self.assertEqual(sizes, [2, 8])
        self.assertEqual(detail, {
            'reason': 'candidates_unverified', 'scope': SCOPE, 'generation': 'g1',
            'snapshot': binding.catalog_scan_id, 'limit': 2, 'rounds': 2,
            'fetched': [2, 8], 'candidates': 6, 'dropped': {'hash_mismatch': 6}})
        # limit at A-int's cap leaves no room for a second round.
        with self.counted_fetches() as sizes:
            self.assert_pending(lambda: self.query(binding, 'shared', limit=100))
        self.assertEqual(sizes, [100])

    def test_query_and_file_budgets(self):
        binding, ranked = self.shared_corpus()
        with self.pinned() as token:
            query = CandidateQuery(self.index, token, binding, max_query_files=1)
            self.addCleanup(query.close)
            rows = query.search('shared', 2)
            stats = query.finish()
        self.assertEqual([row['path'] for row in rows], ranked[:1])
        self.assertEqual((stats['files_opened'], stats['rounds'], stats['memo_hits']),
                         (1, 2, 2))
        self.assertEqual(stats['dropped'], {'query_budget': 6})

        detail = self.assert_pending(
            lambda: self.query(binding, 'shared', limit=2, max_file_bytes=5))
        self.assertEqual(detail['dropped'], {'file_budget': 6})
        size = (self.plans / 's0.md').stat().st_size
        detail = self.assert_pending(
            lambda: self.query(binding, 'shared', limit=2, max_query_bytes=size - 1))
        self.assertEqual(detail['dropped'], {'query_budget': 6})


class CapacityTests(CandidateCase):
    def test_large_corpus_verifies_only_returned_candidates(self):
        size = 25_000
        for d in range(11):
            for f in range(19):
                n = d * 19 + f
                self.write(self.plans, f'part{d:02d}/doc{f:02d}.md',
                           sized(f'# Doc {n}\nmarker{n:04d}token\n', size))
        self.write(self.wiki, 'card.md', '# Card\nwiki side\n')
        self.assertGreater(209 * size, 5_000_000)
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            scan_collections(self.roots, SCOPE, self.index)  # port whole-corpus cap
        catalog = self.scanned_catalog()
        self.assertEqual(catalog.status()['current']['files'], 210)
        binding = self.publish_bound(catalog, 'g1', None)
        catalog.close()  # the query path never consults the catalog

        target = self.plans / 'part07' / 'doc17.md'
        with self.no_enumeration() as calls:
            with self.pinned() as token:
                query = CandidateQuery(self.index, token, binding)
                self.addCleanup(query.close)
                rows = query.search('marker0150token', 5)
                stats = query.finish()
        self.assertEqual(calls, [])
        self.assertEqual([row['path'] for row in rows], ['plans/part07/doc17.md'])
        self.assertEqual(rows[0]['body'], target.read_text(encoding='utf-8'))
        self.assertEqual((stats['files_opened'], stats['bytes_read'], stats['rounds']),
                         (1, size, 1))

    def test_duplicate_path_is_verified_once_across_phases(self):
        _catalog, binding = self.basic()
        alpha_bytes, card_bytes = self.alpha.read_bytes(), self.card.read_bytes()
        with self.pinned() as token:
            query = CandidateQuery(self.index, token, binding)
            self.addCleanup(query.close)
            wiki = query.search('alpha', 5, collection_names=('wiki',))
            raw = query.search('alpha', 5, collection_names=('plans',))
            self.assertEqual([row['path'] for row in wiki + raw],
                             ['wiki/card.md', 'plans/alpha.md'])
            self.assertEqual(query.stats['files_opened'], 2)
            # A vector-style phase over the same pin (hash scores expanded by A-int).
            vector = query.phase(lambda n: self.index.search_scored(
                token, {sha(alpha_bytes): 0.1, sha(card_bytes): 0.2}, n), 5)
            self.assertEqual([row['path'] for row in vector],
                             ['plans/alpha.md', 'wiki/card.md'])
            lexical = query.search('alpha', 5)
            self.assertEqual(sorted(row['path'] for row in lexical),
                             ['plans/alpha.md', 'wiki/card.md'])
            # Same path, different row hash: dropped from the memo without a read.
            detail = self.assert_pending(lambda: query.phase(
                lambda n: [{'scope': SCOPE, 'generation': 'g1', 'path': 'plans/alpha.md',
                            'hash': '0' * 64, 'score': 0.0}], 5))
            self.assertEqual(detail['dropped'], {'hash_mismatch': 1})
            stats = query.finish()
        self.assertEqual(stats['files_opened'], 2)
        self.assertEqual(stats['bytes_read'], len(alpha_bytes) + len(card_bytes))
        self.assertEqual(stats['memo_hits'], 5)
        with self.assertRaisesRegex(IndexContractError, 'candidate_query_closed'):
            query.search('alpha', 5)


if __name__ == '__main__':
    unittest.main()
