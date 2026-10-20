#!/usr/bin/env python3
"""Disposable v6 vector contracts; all embeddings are fixture-supplied floats."""
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))

from laya_index_a import IndexContractError, LayaIndexA
import laya_index_vector
from laya_index_vector import (LayaVectorIndex, profile_identity,
                               qmd_markdown_title, search_vector_verified,
                               source_evidence, verify_generation_seal,
                               _OrderedRowsDigest, _rows_digest)
from laya_index_source_port import SourceStale, scan


FINGERPRINT = 'a' * 64
EVIDENCE = 'b' * 64


class VectorChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-vector-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'index.sqlite3'
        self.index = LayaIndexA(self.path, create=True)
        self.addCleanup(self.index.close)
        self.vectors = LayaVectorIndex(self.index)
        self.profile = self.vectors.register_profile('synthetic-model', FINGERPRINT, 2)

    def h(self, body):
        return hashlib.sha256(body.encode('utf-8')).hexdigest()

    def chunk(self, body, vector, seq=0, pos=0, profile=None, title=None):
        if title is None:
            path = self.index.db.execute('''SELECT path FROM membership WHERE hash=?
                ORDER BY path COLLATE BINARY LIMIT 1''', (self.h(body),)).fetchone()[0]
            title = qmd_markdown_title(body, path)
        evidence = source_evidence(self.h(body), title)
        identity = profile or self.profile
        answer = self.vectors.insert_chunk(self.h(body), identity, seq, pos,
                                           vector, evidence, title=title)
        if seq == 0:
            self.vectors.seal_chunk_set(self.h(body), title, identity, 1)
        return answer

    def stage(self, gen, scope, docs):
        self.assertEqual(self.index.stage(gen, scope, docs), 'prepared')

    def seal_publish(self, gen, scope, docs, claim, base=None, vectors=None):
        self.stage(gen, scope, docs)
        for body, vec in (vectors or {}).items():
            self.chunk(body, vec)
        self.vectors.seal_generation(gen, self.profile)
        self.assertEqual(self.index.publish(base, gen, claim, require_vector=True), 'published')
        return self.index.pin(scope)[0]

    def query(self, token, vector=(1.0, 0.0), **kwargs):
        return self.vectors.search_exact(token, vector, model_id='synthetic-model',
             embedding_fingerprint=FINGERPRINT, dimension=2, limit=5, **kwargs)

    def test_scope_generation_filter_precedes_exact_top_k(self):
        shared = '# eligible shared body'
        alpha = [(f'a-{i:03}.md', shared, True) for i in range(8)]
        alpha += [('hidden.md', 'hidden body', False)]
        self.stage('alpha-0', 'alpha', alpha)
        self.chunk(shared, (0.8, 0.6))
        self.vectors.seal_generation('alpha-0', self.profile)
        self.assertEqual(self.index.publish(None, 'alpha-0', 'alpha-claim',
                                            require_vector=True), 'published')
        other = [(f'z-{i:03}.md', f'foreign closer {i}', True) for i in range(200)]
        self.stage('other-0', 'other', other)
        for _, body, _ in other:
            self.chunk(body, (1.0, 0.0))
        self.vectors.seal_generation('other-0', self.profile)
        self.index.publish(None, 'other-0', 'other-claim', require_vector=True)
        pin = self.index.pin('alpha')[0]
        rows = self.query(pin)
        self.assertEqual([r['path'] for r in rows], [f'a-{i:03}.md' for i in range(5)])
        self.assertTrue(all(r['generation'] == 'alpha-0' and r['scope'] == 'alpha' for r in rows))

    def test_generation_size_is_not_a_query_or_seal_admission_cap(self):
        docs = [(f'doc-{i}.md', f'body {i}', True) for i in range(3)]
        self.stage('g0', 'alpha', docs)
        for _, body, _ in docs:
            self.chunk(body, (1.0, 0.0))
        # Each document has one bounded chunk set. The generation as a whole
        # must remain sealable and searchable beyond the old candidate cap.
        with mock.patch('laya_index_vector.MAX_CANDIDATES', 2):
            self.assertEqual(self.vectors.seal_generation('g0', self.profile)['rows_count'], 3)
            self.assertEqual(self.index.publish(None, 'g0', 'claim',
                                                require_vector=True), 'published')
            pin = self.index.pin('alpha')[0]
            self.assertEqual(len(self.query(pin)), 3)
            with self.assertRaisesRegex(IndexContractError, 'vector_budget_exceeded'):
                self.query(pin, max_candidates=2)

    def test_streamed_generation_digest_matches_existing_v2_seal(self):
        rows = [('a.md', 'a' * 64, '한글', 0, 'b' * 64),
                ('a.md', 'a' * 64, '한글', 1, 'c' * 64),
                ('z.md', 'd' * 64, 'Z', 0, 'e' * 64)]
        streamed = _OrderedRowsDigest('g0', 'scope', self.profile)
        for row in rows:
            streamed.add(row)
        self.assertEqual(streamed.hexdigest(),
                         _rows_digest('g0', 'scope', self.profile, rows))

    def test_collection_filter_precedes_vector_budget_and_top_k(self):
        docs = [(f'raw/a-{i}.md', f'raw {i}', True) for i in range(8)]
        docs += [('wiki/target.md', '# Target\nwiki', True)]
        self.stage('g0', 'alpha', docs)
        for path, body, _ in docs:
            self.chunk(body, (1.0, 0.0) if path.startswith('raw/') else (0.0, 1.0))
        self.vectors.seal_generation('g0', self.profile)
        self.index.publish(None, 'g0', 'claim', require_vector=True)
        pin = self.index.pin('alpha')[0]
        rows = self.query(pin, max_candidates=1, path_prefixes=('wiki/',))
        self.assertEqual([r['path'] for r in rows], ['wiki/target.md'])
        with self.assertRaisesRegex(IndexContractError, 'invalid_collection_filter'):
            self.query(pin, path_prefixes=('../',))

    def test_model_fingerprint_dimension_and_chunk_input_are_exact(self):
        self.seal_publish('g0', 'alpha', [('a.md', 'body one', True)], 'c0',
                          vectors={'body one': (1.0, 0.0)})
        pin = self.index.pin('alpha')[0]
        self.assertEqual(profile_identity('synthetic-model', FINGERPRINT, 2), self.profile)
        for changes in ({'model_id': 'different'},
                        {'embedding_fingerprint': 'c'*64}, {'dimension': 3}):
            args = dict(model_id='synthetic-model', embedding_fingerprint=FINGERPRINT,
                        dimension=2, limit=5)
            args.update(changes)
            with self.assertRaisesRegex(IndexContractError, 'vector_profile_mismatch|invalid_vector'):
                self.vectors.search_exact(pin, [1.0, 0.0, 0.0] if changes.get('dimension') == 3
                    else [1.0, 0.0], **args)
        with self.assertRaisesRegex(IndexContractError, 'invalid_vector'):
            self.query(pin, (float('nan'), 1.0))
        with self.assertRaisesRegex(IndexContractError, 'invalid_vector'):
            self.chunk('body one', (0.0, 0.0), seq=1)
        with self.assertRaisesRegex(IndexContractError, 'vector_chunk_conflict'):
            self.chunk('body one', (0.0, 1.0))

    def test_sealed_vector_rows_are_immutable_across_new_generation(self):
        body = 'shared reuse'
        old = self.seal_publish('g0', 'alpha', [('a.md', body, True)], 'c0',
                                vectors={body: (0.0, 1.0)})
        self.assertAlmostEqual(self.query(old)[0]['score'], 1.0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.chunk(body, (1.0, 0.0), seq=1)
        self.stage('g1', 'alpha', [('a.md', body, True)])
        self.vectors.seal_generation('g1', self.profile)
        self.index.publish('g0', 'g1', 'c1', require_vector=True)
        new = self.index.pin('alpha')[0]
        self.assertEqual(self.query(old)[0]['seq'], 0)
        self.assertAlmostEqual(self.query(old)[0]['score'], 1.0)
        self.assertEqual(self.query(new)[0]['seq'], 0)
        self.assertAlmostEqual(self.query(new)[0]['score'], 1.0)
        self.assertEqual(self.index.publish('g0', 'g1', 'c1', require_vector=True), 'replayed')

    def test_unsealed_missing_coverage_and_post_seal_mutation_fail(self):
        self.stage('g0', 'alpha', [('a.md', 'unembedded', True)])
        with self.assertRaisesRegex(IndexContractError, 'vector_coverage_incomplete'):
            self.vectors.seal_generation('g0', self.profile)
        self.assertEqual(self.index.db.execute(
            'SELECT COUNT(*) FROM gen_vector_rows').fetchone()[0], 0)
        with self.assertRaisesRegex(IndexContractError, 'vector_generation_unsealed'):
            self.index.publish(None, 'g0', 'c0', require_vector=True)
        self.chunk('unembedded', (1.0, 0.0))
        self.vectors.seal_generation('g0', self.profile)
        self.assertEqual(self.vectors.seal_generation('g0', self.profile)['rows_count'], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.db.execute('INSERT INTO gen_vector_rows VALUES(?,?,?,?,?,?,?)',
                ('g0', 'alpha', 'a.md', self.h('unembedded'), 'a', self.profile, 0))
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.db.execute('UPDATE vector_chunks SET pos=1')

    def test_exclusion_and_cost_budget_fail_closed(self):
        pin = self.seal_publish('g0', 'alpha',
             [('a.md', 'one', True), ('b.md', 'two', True)], 'c0',
             vectors={'one': (1.0, 0.0), 'two': (0.0, 1.0)})
        with self.assertRaisesRegex(IndexContractError, 'vector_budget_exceeded'):
            self.query(pin, max_candidates=1)
        with self.assertRaisesRegex(IndexContractError, 'vector_budget_exceeded'):
            self.query(pin, max_float_work=3)
        self.index.exclude('alpha', path='a.md')
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.query(pin)

    def test_expired_deadline_stops_before_seal_row_work(self):
        docs = [(f'doc-{i}.md', f'body {i}', True) for i in range(4)]
        pin = self.seal_publish('g0', 'alpha', docs, 'c0',
                                vectors={body: (1.0, 0.0) for _, body, _ in docs})
        self.assertEqual(len(self.query(pin, deadline_seconds=30)), 4)
        clock = mock.Mock()
        # The first reading sets the 2s deadline; every later reading is past it.
        clock.monotonic.side_effect = [0.0] + [10.0] * 10_000
        with mock.patch.object(laya_index_vector, 'time', clock), \
             mock.patch.object(laya_index_vector, '_read_chunk_set') as chunk_sets, \
             mock.patch.object(laya_index_vector, '_provenance') as provenance:
            with self.assertRaisesRegex(IndexContractError, 'vector_deadline_exceeded'):
                self.query(pin, deadline_seconds=2)
        chunk_sets.assert_not_called()
        provenance.assert_not_called()
        self.assertFalse(self.index.db.in_transaction)
        # The handler is removed and the unchanged pin still proves its seal.
        self.assertEqual(len(self.query(pin)), 4)

    def test_deadline_interrupts_seal_proof_midway_and_fails_closed(self):
        docs = [(f'doc-{i}.md', f'body {i}', True) for i in range(6)]
        pin = self.seal_publish('g0', 'alpha', docs, 'c0',
                                vectors={body: (1.0, 0.0) for _, body, _ in docs})
        real = laya_index_vector._read_chunk_set
        reads = []
        def counted(*args, **kwargs):
            reads.append(kwargs.get('deadline'))
            return real(*args, **kwargs)
        clock = mock.Mock()
        # Time runs out once the proof has read its first per-path chunk set.
        clock.monotonic.side_effect = lambda: 10.0 if reads else 0.0
        with mock.patch.object(laya_index_vector, 'time', clock), \
             mock.patch.object(laya_index_vector, '_read_chunk_set', side_effect=counted):
            with self.assertRaisesRegex(IndexContractError, 'vector_deadline_exceeded'):
                self.query(pin, deadline_seconds=2)
        self.assertEqual(reads, [2.0])
        self.assertFalse(self.index.db.in_transaction)
        # Publish-time verification carries no deadline and still checks all rows.
        self.assertEqual(verify_generation_seal(self.index, 'g0')['rows_count'], 6)

    def test_cross_connection_pin_and_reopen(self):
        old = self.seal_publish('g0', 'alpha', [('a.md', 'old', True)], 'c0',
                                vectors={'old': (0.0, 1.0)})
        second = LayaIndexA(self.path)
        try:
            self.assertEqual(self.index.stage('g1', 'alpha', [('b.md', 'new', True)]), 'prepared')
            self.chunk('new', (1.0, 0.0))
            self.vectors.seal_generation('g1', self.profile)
            self.index.publish('g0', 'g1', 'c1', require_vector=True)
            self.assertEqual([r['path'] for r in LayaVectorIndex(second).search_exact(
                old, [1, 0], model_id='synthetic-model',
                embedding_fingerprint=FINGERPRINT, dimension=2, limit=5)], ['a.md'])
            self.assertEqual([r['path'] for r in self.query(self.index.pin('alpha')[0])], ['b.md'])
            self.assertEqual(second.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            second.close()

    def test_source_verified_rows_and_changed_source(self):
        source_root = (Path(self.tmp.name) / 'source').resolve()
        source_root.mkdir()
        (source_root / 'a.md').write_text('live source', encoding='utf-8')
        source = scan(str(source_root), 'alpha', self.index)
        self.index.stage('g0', 'alpha', source.documents(), base=None,
                         source_manifest=source.manifest, claim_digest='c'*64,
                         claim_epoch=1)
        self.chunk('live source', (1.0, 0.0))
        self.vectors.seal_generation('g0', self.profile)
        self.index.publish(None, 'g0', 'c0', source_manifest=source.manifest,
                           claim_digest='c'*64, claim_epoch=1, require_vector=True)
        pin = self.index.pin('alpha')[0]
        args = dict(model_id='synthetic-model', embedding_fingerprint=FINGERPRINT,
                    dimension=2, limit=5)
        rows = search_vector_verified(self.index, str(source_root), pin, [1, 0], **args)
        self.assertEqual([r['path'] for r in rows], ['a.md'])
        (source_root / 'a.md').write_text('changed source', encoding='utf-8')
        with self.assertRaises(SourceStale):
            search_vector_verified(self.index, str(source_root), pin, [1, 0], **args)

    def test_direct_sql_blob_and_seal_forgery_are_rejected(self):
        self.stage('g0', 'alpha', [('a.md', 'one', True)])
        hash_ = self.h('one')
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.db.execute('INSERT INTO vector_chunks VALUES(?,?,?,?,?,?,?,?)',
                (hash_, 'a', self.profile, 0, 0, b'bad', EVIDENCE, 'c'*64))
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.db.execute('INSERT INTO gen_vector_seals VALUES(?,?,?,?,?)',
                ('g0', 'alpha', self.profile, 'c'*64, 0))
        self.chunk('one', (1, 0))
        self.vectors.seal_generation('g0', self.profile)
        self.assertEqual(verify_generation_seal(self.index, 'g0')['rows_count'], 1)

    def test_partial_prepared_row_replay_accepts_only_exact_rows(self):
        self.stage('g0', 'alpha', [('a.md', 'one', True), ('b.md', 'two', True)])
        self.chunk('one', (1, 0))
        self.chunk('two', (0, 1))
        # Simulate a prior owner that committed one prepared binding before
        # dying. The next attempt must reconcile rather than duplicate it.
        self.index.db.execute('INSERT INTO gen_vector_rows VALUES(?,?,?,?,?,?,?)',
                              ('g0', 'alpha', 'a.md', self.h('one'), 'a', self.profile, 0))
        self.assertEqual(self.vectors.seal_generation('g0', self.profile)['rows_count'], 2)
        self.assertEqual(self.index.publish(None, 'g0', 'claim', require_vector=True),
                         'published')

    def test_same_generation_rejects_a_path_bound_to_other_title(self):
        body = 'same body, filename supplies QMD title'
        self.stage('g0', 'alpha', [('a.md', body, True), ('b.md', body, True)])
        self.chunk(body, (1.0, 0.0), title='a')
        self.chunk(body, (0.0, 1.0), title='b')
        # SQL's structural trigger permits a prepared row only when both its
        # membership and immutable chunk exist. The semantic seal must still
        # reject a valid chunk belonging to a different title/path.
        self.index.db.execute('INSERT INTO gen_vector_rows VALUES(?,?,?,?,?,?,?)',
            ('g0', 'alpha', 'b.md', self.h(body), 'a', self.profile, 0))
        with self.assertRaisesRegex(IndexContractError, 'vector_row_conflict'):
            self.vectors.seal_generation('g0', self.profile)
        with self.assertRaisesRegex(IndexContractError, 'vector_generation_unsealed'):
            self.index.publish(None, 'g0', 'claim', require_vector=True)


if __name__ == '__main__':
    unittest.main()
