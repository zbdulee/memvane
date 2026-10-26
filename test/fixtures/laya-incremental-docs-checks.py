"""Synthetic per-document publication checks; no host, model or live data."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))

from laya_index_a import LayaIndexA
from laya_index_capacity import GenerationBindingStore
from laya_index_claim_queue import ClaimQueue
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import sync_step
from laya_source_catalog import LayaSourceCatalog
import laya_incremental_docs as incremental
import laya_vector_capacity as capacity


SCOPE = 'c' * 32


class Provider:
    profile = {'model_id': 'synthetic-only',
               'embedding_fingerprint': 'f' * 64, 'dimension': 2}
    manifest = {'recipeId': capacity.CANDIDATE_RECIPE}

    def __init__(self):
        self.calls = []

    def embed_document_chunks(self, body, path):
        self.calls.append(path)
        return [{'seq': seq, 'pos': seq * 32, 'vector': [1.0, 0.0]}
                for seq in range(3 if 'many chunks' in body else 1)]


class IncrementalDocsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='laya-incremental-docs-')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.project = base / 'project'
        self.wiki = self.project / '.auto-context' / 'wiki'
        self.wiki.mkdir(parents=True)
        self.state = base / 'private'
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o700)
        self.paths = {'db_path': str(self.state / 'index.sqlite'),
                      'queue_path': str(self.state / 'queue.sqlite'),
                      'catalog_path': str(self.state / 'catalog.sqlite'),
                      'binding_store_path': str(self.state / 'bindings.sqlite')}
        index = LayaIndexA(self.paths['db_path'], create=True)
        incarnation = index.incarnation()
        index.close()
        ClaimQueue.create(self.paths['queue_path'], index_incarnation=incarnation).close()
        roots = {'wiki': str(self.wiki)}
        LayaSourceCatalog(self.paths['catalog_path'], roots, SCOPE, create=True).close()
        GenerationBindingStore(self.paths['binding_store_path'], scope=SCOPE,
                               db_incarnation=incarnation, roots=roots, create=True).close()
        self.provider = Provider()
        self.profile = capacity.provider_profile_id(self.provider)
        self.identity = {'projectRoot': str(self.project), 'stateRoot': str(self.state),
                         'installId': 'a' * 32, 'scopeId': SCOPE,
                         'dbIncarnation': incarnation,
                         'vectorProfileId': self.profile,
                         'candidateDb': self.paths['db_path'],
                         'collections': [
                             {'name': 'raw', 'role': 'raw',
                              'sourceRoot': str(self.project)},
                             {'name': 'wiki', 'role': 'wiki',
                              'sourceRoot': str(self.wiki)}]}
        self.runtime = LayaIndexRuntime(source_root=roots, scope=SCOPE,
                                        db_incarnation=incarnation, **self.paths)
        self.addCleanup(self.runtime.close)

    def write(self, name, body):
        (self.wiki / name).write_text(body, encoding='utf-8')

    def snapshot(self):
        prior = self.runtime.catalog.status()['current']
        for _ in range(100):
            result = sync_step(self.runtime, before_publish=lambda: None,
                               require_vector=True, vector_profile_id=self.profile)
            if result.get('reason') == 'vector_backlog':
                return
            current = self.runtime.catalog.status()['current']
            if (prior is not None and current is not None
                    and current['scan_id'] != prior['scan_id']):
                # An identical rescan may already match the published head;
                # no vector_backlog answer is needed to fill its cached rows.
                return
        self.fail('catalog did not complete')

    def publish(self, snapshot, path, sha, title, body, profile_id):
        incremental.publish_document(self.runtime, self.identity, snapshot, path,
                                     sha, title, body, profile_id)

    def rows(self):
        path = self.state / incremental.FILE
        if not path.exists():
            return []
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            return db.execute('SELECT path,hash,chunk_count FROM docs ORDER BY path').fetchall()

    def test_first_completed_document_visible_before_full_generation(self):
        self.write('a.md', '# A\nalpha\n')
        self.write('b.md', '# B\nbeta\n')
        self.snapshot()
        first = capacity.vector_fill_step(self.runtime, self.provider, max_files=1,
                                          on_document_complete=self.publish)
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.runtime.index.db.execute('SELECT COUNT(*) FROM head').fetchone()[0],
                         0)
        remaining = capacity.vector_fill_step(self.runtime, self.provider, max_files=1,
                                              on_document_complete=self.publish)
        self.assertEqual(remaining['state'], 'complete')
        self.assertEqual(len(self.rows()), 2)

    def test_hashed_document_visible_before_catalog_enumeration_finishes(self):
        self.write('z.md', '# Z\nfirst ready\n')
        (self.wiki / 'a').mkdir()
        (self.wiki / 'a' / 'early.md').write_text('# Early\nfound later\n',
                                                   encoding='utf-8')
        (self.wiki / 'b').mkdir()
        (self.wiki / 'b' / 'later.md').write_text('# Later\nnot scanned\n',
                                                   encoding='utf-8')
        self.runtime.catalog.request_scan()
        first_step = self.runtime.catalog.step(max_files=2)
        self.assertEqual(first_step['status'], 'pending')
        self.assertEqual(self.runtime.catalog.status()['active']['phase'], 'enumerate')
        first = capacity.pending_vector_fill_step(
            self.runtime, self.provider, on_document_complete=self.publish)
        self.assertEqual(first['embedded'], 1)
        self.assertEqual([row[0] for row in self.rows()], ['wiki/z.md'])
        self.assertIsNone(self.runtime.catalog.status()['current'])
        self.assertEqual(self.runtime.index.db.execute(
            'SELECT COUNT(*) FROM head').fetchone()[0], 0)
        # The next directory reveals a lexically earlier path. The ordinal
        # cursor must still publish it during the same unfinished scan.
        self.assertEqual(self.runtime.catalog.step(max_files=2)['status'], 'pending')
        second = capacity.pending_vector_fill_step(
            self.runtime, self.provider, on_document_complete=self.publish)
        self.assertEqual(second['embedded'], 1)
        self.assertEqual([row[0] for row in self.rows()],
                         ['wiki/a/early.md', 'wiki/z.md'])
        self.assertIsNone(self.runtime.catalog.status()['current'])

    def test_multichunk_document_publishes_only_after_complete_seal(self):
        self.write('many.md', '# Many\nmany chunks\n')
        self.snapshot()
        with mock.patch.object(incremental, 'publish_document',
                               side_effect=RuntimeError('after vector commit')):
            with self.assertRaisesRegex(RuntimeError, 'after vector commit'):
                capacity.vector_fill_step(self.runtime, self.provider,
                                          on_document_complete=self.publish)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.runtime.index.db.execute(
            'SELECT rows_count FROM vector_chunk_sets').fetchone()[0], 3)
        complete = capacity.vector_fill_step(self.runtime, self.provider,
                                             on_document_complete=self.publish)
        self.assertEqual(complete['state'], 'complete')
        self.assertEqual(self.rows()[0][2], 3)

    def test_pending_multichunk_replays_without_exposing_partial_rows(self):
        self.write('many.md', '# Many\nmany chunks\n')
        (self.wiki / 'later').mkdir()
        self.runtime.catalog.request_scan()
        self.assertEqual(self.runtime.catalog.step(max_files=2)['status'], 'pending')
        with mock.patch.object(incremental, 'publish_document',
                               side_effect=RuntimeError('before sidecar commit')):
            with self.assertRaisesRegex(RuntimeError, 'before sidecar commit'):
                capacity.pending_vector_fill_step(
                    self.runtime, self.provider, on_document_complete=self.publish)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.runtime.index.db.execute(
            'SELECT rows_count FROM vector_chunk_sets').fetchone()[0], 3)
        replay = capacity.pending_vector_fill_step(
            self.runtime, self.provider, on_document_complete=self.publish)
        self.assertEqual(replay['cached'], 1)
        self.assertEqual(self.rows()[0][2], 3)
        self.assertEqual(self.provider.calls, ['wiki/many.md'])

    def test_crash_after_vector_commit_replays_without_model_call(self):
        self.write('a.md', '# A\nalpha\n')
        self.snapshot()
        with mock.patch.object(incremental, 'publish_document',
                               side_effect=RuntimeError('synthetic power loss')):
            with self.assertRaisesRegex(RuntimeError, 'power loss'):
                capacity.vector_fill_step(self.runtime, self.provider,
                                          on_document_complete=self.publish)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.provider.calls, ['wiki/a.md'])
        again = capacity.vector_fill_step(self.runtime, self.provider,
                                          on_document_complete=self.publish)
        self.assertEqual((again['state'], again['cached']), ('complete', 1))
        self.assertEqual(self.provider.calls, ['wiki/a.md'])
        self.assertEqual(len(self.rows()), 1)

    def test_upgraded_candidate_backfills_old_vectors_without_model(self):
        self.write('a.md', '# A\nalpha\n')
        self.write('b.md', '# B\nbeta\n')
        self.snapshot()
        old = capacity.vector_fill_step(self.runtime, self.provider)
        self.assertEqual(old['state'], 'complete')
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.rows(), [])
        original = incremental.publish_document

        def interrupted(*args):
            if args[3] == 'wiki/b.md':
                raise RuntimeError('synthetic interruption')
            return original(*args)

        options = {'deadline': 100, 'clock': lambda: 0,
                   'before_publish': lambda: None, 'should_yield': lambda: False}
        with mock.patch.object(incremental, 'publish_document', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
                incremental.backfill_cached(self.runtime, self.identity,
                                            self.profile, **options)
        self.assertEqual([row[0] for row in self.rows()], ['wiki/a.md'])
        done = incremental.backfill_cached(self.runtime, self.identity,
                                           self.profile, **options)
        self.assertEqual(done['state'], 'complete')
        self.assertEqual([row[0] for row in self.rows()], ['wiki/a.md', 'wiki/b.md'])
        self.assertEqual(self.provider.calls, ['wiki/a.md', 'wiki/b.md'])

    def test_backfill_requests_rescan_if_source_changed(self):
        self.write('a.md', '# A\nalpha\n')
        self.snapshot()
        self.assertEqual(capacity.vector_fill_step(self.runtime, self.provider)['state'],
                         'complete')
        self.write('a.md', '# A\nchanged\n')
        result = incremental.backfill_cached(
            self.runtime, self.identity, self.profile, deadline=100,
            clock=lambda: 0, before_publish=lambda: None,
            should_yield=lambda: False)
        self.assertEqual(result, {'state': 'pending', 'reason': 'source_changed'})
        self.assertEqual(self.rows(), [])

    def test_backfill_refuses_missing_cache_behind_fill_cursor(self):
        self.write('a.md', '# A\nalpha\n')
        self.snapshot()
        self.assertEqual(capacity.vector_fill_step(self.runtime, self.provider)['state'],
                         'complete')
        with mock.patch.object(incremental.LayaVectorIndex, 'cached_chunk_set',
                               return_value=None):
            with self.assertRaisesRegex(Exception, 'incremental_cached_member_missing'):
                incremental.backfill_cached(
                    self.runtime, self.identity, self.profile, deadline=100,
                    clock=lambda: 0, before_publish=lambda: None,
                    should_yield=lambda: False)
        self.assertEqual(self.rows(), [])

    def test_wrong_scope_cannot_publish(self):
        self.write('a.md', '# A\nalpha\n')
        self.snapshot()
        wrong = {**self.identity, 'scopeId': 'd' * 32}
        callback = lambda snapshot, path, sha, title, body, profile: (
            incremental.publish_document(self.runtime, wrong, snapshot, path,
                                         sha, title, body, profile))
        with self.assertRaisesRegex(Exception, 'incremental_document_identity_changed'):
            capacity.vector_fill_step(self.runtime, self.provider,
                                      on_document_complete=callback)
        self.assertEqual(self.rows(), [])

    def test_same_path_and_bytes_after_metadata_change_reuses_vector(self):
        self.write('a.md', '# A\nalpha\n')
        self.snapshot()
        capacity.vector_fill_step(self.runtime, self.provider,
                                  on_document_complete=self.publish)
        original = self.rows()
        source = self.wiki / 'a.md'
        current = source.stat()
        os.utime(source, ns=(current.st_atime_ns, current.st_mtime_ns + 10_000_000))
        self.runtime.request_scan()
        self.snapshot()
        again = capacity.vector_fill_step(self.runtime, self.provider,
                                          on_document_complete=self.publish)
        self.assertEqual(again['cached'], 1)
        self.assertEqual(self.provider.calls, ['wiki/a.md'])
        self.assertEqual(self.rows(), original)


if __name__ == '__main__':
    unittest.main()
