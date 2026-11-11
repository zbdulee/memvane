"""Synthetic contracts for source-trusted counts and bounded candidate sampling."""
from contextlib import ExitStack
import copy
import importlib
import importlib.util
import hashlib
import sqlite3
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('parity_driver', HERE / 'laya-shadow-driver-checks.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class ParityReplacementChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = driver.DriverChecks('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.seed, self.case = self.fixture.seeded()
        self.worker, self.migration = self.fixture.W, self.fixture.M
        self.view_type = importlib.import_module('laya_view_store').ViewStore
        self.v6 = importlib.import_module('laya_v6_reader')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(self.fixture.authority(self.seed, self.case))
        self.fixture.to_parity(self.seed, self.case)
        self.stack.enter_context(mock.patch.object(self.worker, 'SHADOW_STEPS_PER_PASS', 1))

    def drive(self):
        observations = []
        for _ in range(20):
            result = self.worker.shadow_pass(self.case.project)
            observations.append((result['state'], result.get('reason'), result.get('stages')))
            if result['state'] == 'error' or self.fixture.intent(self.case)['migration_state'] == 'shadow_verified':
                break
        return observations

    def test_real_candidate_search_and_old_bundle_fail_closed(self):
        legacy_spec = importlib.util.spec_from_file_location(
            'old_shadow_bundle', HERE / 'laya-shadow-migration-bb4ae489.py')
        old = importlib.util.module_from_spec(legacy_spec)
        legacy_spec.loader.exec_module(old)
        first = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(first['state'], 'error', first)
        partial = self.fixture.intent(self.case)['parity']
        self.assertGreaterEqual(partial['page_members'], 4)
        with self.assertRaises(ValueError):
            old._check_parity_record(partial)
        self.assertFalse(partial['compared'])
        self.assertIsNotNone(partial['cursor'])
        observed = self.drive()
        self.assertEqual(self.fixture.intent(self.case)['migration_state'], 'shadow_verified', observed)
        final = self.fixture.intent(self.case)['parity']
        self.assertEqual(final['prompts'], min(final['members'], 10))
        self.assertEqual(final['pages'], final['prompts'])
        self.assertIsNone(final['cursor'])
        self.assertIsNotNone(self.fixture.receipt(self.case))
        self.assertIsNone(old.shadow_parity_receipt(
            self.case.state, candidate=self.case.state / self.worker.SHADOW_CANDIDATE,
            candidate_incarnation=self.fixture.incarnation(self.case)))
        events = self.fixture.events(self.case)
        reports = [event for event in events if event.get('sampled') is not None]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['sampled'], final['prompts'])
        self.assertEqual(reports[0]['top_hits'], final['page_members'] - 4)

    def test_actual_new_v6_head_does_not_change_pinned_sample(self):
        first = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(first['state'], 'error', first)
        old = self.fixture.intent(self.case)['parity']
        old_head = self.case.head()[0]
        page = next((self.case.auto / 'wiki').rglob('*.md'))
        page.write_text(page.read_text(encoding='utf8') + '\nSynthetic newer content.\n', encoding='utf8')
        roots = {self.case.WIKI: str(self.case.auto / 'wiki')}
        with self.case.runtime(roots) as runtime:
            runtime.catalog.request_scan()
        self.case.publish_all(roots)
        self.assertNotEqual(self.case.head()[0], old_head)
        for _ in range(10):
            if self.fixture.intent(self.case)['parity']['compared']:
                break
            status = self.worker.shadow_pass(self.case.project)
            self.assertNotEqual(status['state'], 'error', status)
        parity = self.fixture.intent(self.case)['parity']
        self.assertTrue(parity['compared'])
        self.assertEqual(parity['source_revision'], old['source_revision'])
        self.assertEqual(parity['generation'], old['generation'])
        self.assertIsNone(parity['seal'])
        status = self.worker.shadow_pass(self.case.project)
        current = self.fixture.intent(self.case)
        self.assertNotEqual(current['migration_state'], 'shadow_verified', status)
        self.assertTrue(current.get('restart_required'), status)
        for _ in range(100):
            status = self.worker.shadow_pass(self.case.project)
            self.assertNotEqual(status['state'], 'error', status)
            current = self.fixture.intent(self.case)
            if current['migration_state'] == 'shadow_verified':
                break
        self.assertEqual(current['migration_state'], 'shadow_verified', status)
        self.assertEqual(current['source_revision']['generation'], self.case.head()[0])
        self.assertIsNotNone(current['parity']['seal'])

    def test_sampling_does_not_reopen_v6_snapshot(self):
        first = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(first['state'], 'error', first)
        readers = importlib.import_module('laya_reader_open')
        with mock.patch.object(readers, 'inspect_warm_snapshot',
                               side_effect=AssertionError('source re-opened')):
            for _ in range(10):
                if self.fixture.intent(self.case)['parity']['compared']:
                    break
                result = self.worker.shadow_pass(self.case.project)
                self.assertNotEqual(result['state'], 'error', result)
        self.assertTrue(self.fixture.intent(self.case)['parity']['compared'])
        self.assertIsNone(self.fixture.intent(self.case)['parity']['seal'])

    def test_deadline_before_cursor_write_never_reinterprets_marker(self):
        first = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(first['state'], 'error', first)
        before = self.fixture.intent(self.case)['parity']
        self.assertGreaterEqual(before['page_members'], 4)
        original_sample = self.migration._candidate_sample
        original_intent = self.migration._migration_intent
        original_clock = self.migration.time.monotonic
        clock_patches = []
        state = {'sampled': False, 'deadline': None}
        def sampled(*args, **kwargs):
            result = original_sample(*args, **kwargs)
            state.update(sampled=True, deadline=kwargs.get('deadline', args[-1]))
            return result
        def read_intent(*args, **kwargs):
            result = original_intent(*args, **kwargs)
            if state['sampled'] and not clock_patches:
                calls = [0]
                def clock():
                    calls[0] += 1
                    return state['deadline'] + 1 if calls[0] == 1 else original_clock()
                patch = mock.patch.object(self.migration.time, 'monotonic', side_effect=clock)
                patch.start()
                clock_patches.append(patch)
            return result
        try:
            with mock.patch.object(self.migration, '_candidate_sample', side_effect=sampled), \
                 mock.patch.object(self.migration, '_migration_intent', side_effect=read_intent):
                self.worker.shadow_pass(self.case.project)
        finally:
            for patch in clock_patches:
                patch.stop()
        after = self.fixture.intent(self.case)['parity']
        self.assertEqual(after, before)
        self.assertGreaterEqual(after['page_members'], 4)

    def test_search_deadline_failure_preserves_cursor(self):
        first = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(first['state'], 'error', first)
        before = self.fixture.intent(self.case)['parity']
        with mock.patch.object(self.view_type, 'search',
                               side_effect=driver.IndexContractError('reader_snapshot_deadline')):
            self.worker.shadow_pass(self.case.project)
        after = self.fixture.intent(self.case)['parity']
        self.assertEqual(after, before)

    def test_existing_unsealed_record_restarts_in_new_domain(self):
        intent = self.fixture.intent(self.case)
        old = self.migration._initial_parity(intent)
        old.pop('page_members')
        old['pages'] = 1
        old['members'] = 1
        old['prompts'] = 1
        old['cursor'] = {'after': 'synthetic.md', 'observation': old['observation']}
        ledger = importlib.import_module('laya_ledger_common')
        ledger._write(self.case.state / self.migration._INTENT,
                      self.migration._stamp({**intent, 'parity': old}), replace=True)
        result = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(result['state'], 'error', result)
        new = self.fixture.intent(self.case)['parity']
        self.assertEqual(new['attempt'], old['attempt'] + 1)
        self.assertEqual(new['discarded_pages'], 1)
        self.assertGreaterEqual(new['page_members'], 4)
        self.assertNotEqual(new['digest'], old['digest'])

    def test_digest_commits_to_actual_executed_fts_query(self):
        before = self.fixture.intent(self.case)
        initial = self.migration._initial_parity(before)
        queries = []
        real = self.view_type.search
        def record_query(view, pin, query, *args, **kwargs):
            queries.append(query)
            return real(view, pin, query, *args, **kwargs)
        with mock.patch.object(self.view_type, 'search', new=record_query):
            result = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(result['state'], 'error', result)
        actual = self.fixture.intent(self.case)['parity']
        self.assertGreater(len(queries), 0)
        values = importlib.import_module('laya_blob_values')
        expected = values.digest('memvane-shadow-recall-sample-page-v2', {
            'previous': initial['digest'],
            'source_generation': initial['source_revision']['generation'],
            'count': actual['members'],
            'prompt': hashlib.sha256(queries[0].encode('utf8')).hexdigest(),
        })
        self.assertEqual(actual['digest'], expected)

    def test_hit_is_only_counted_within_merged_top_ten(self):
        path = self.case.state / self.worker.SHADOW_CANDIDATE
        with sqlite3.connect(path) as db:
            target = db.execute('SELECT path FROM wiki_members WHERE eligible=1 ORDER BY path LIMIT 1').fetchone()[0]
        def search(_view, _pin, _query, _limit, **kwargs):
            origin = kwargs.get('_legacy_origin')
            rows = [{'path': '%s-%02d.md' % (origin, i), 'score': -float(i + 1)} for i in range(10)]
            if origin == 'base':
                rows[5]['path'] = target
            return rows
        real_read = self.view_type._read_pinned
        def paired(view, *args, **kwargs):
            generation, seal, capture, stage = real_read(view, *args, **kwargs)
            stage = copy.deepcopy(stage)
            stage['legacy']['sidecar_corpus'] = []
            return generation, seal, capture, stage
        with mock.patch.object(self.view_type, 'search', new=search), \
             mock.patch.object(self.view_type, '_read_pinned', new=paired):
            status = self.worker.shadow_pass(self.case.project)
        self.assertNotEqual(status['state'], 'error', status)
        self.assertEqual(self.fixture.intent(self.case)['parity']['page_members'], 4)

    def test_incomplete_source_commitments_block_sample_and_receipt(self):
        intent = self.fixture.intent(self.case)
        self.assertIsNone(intent.get('parity'))
        for defect in ('missing', 'tampered'):
            altered = copy.deepcopy(intent)
            if defect == 'missing':
                altered['verification'] = None
            else:
                altered['corpora']['verification_digest'] = 'f' * 64
            with self.subTest(defect=defect), \
                 mock.patch.object(self.migration, '_migration_intent', return_value=altered), \
                 mock.patch.object(self.migration, '_candidate_sample',
                                   side_effect=AssertionError('sample started')) as sampled:
                store, _ = self.worker._shadow_open(self.case.state, self.case.project, self.case.route['scopeId'])
                try:
                    with self.assertRaises(ValueError):
                        self.migration._advance_parity(store, project=self.case.project,
                            executing_root=self.case.plugin, deadline=time.monotonic() + 3)
                    sampled.assert_not_called()
                finally:
                    store.close()
            self.assertIsNone(self.fixture.intent(self.case).get('parity'))
            self.assertIsNone(self.fixture.receipt(self.case))

    def test_source_trusted_count_mismatch_is_refused(self):
        original = self.view_type._read_pinned
        def missing_candidate(view, *args, **kwargs):
            generation, seal, capture, stage = original(view, *args, **kwargs)
            stage = copy.deepcopy(stage)
            stage['legacy']['paths'].append('synthetic-missing.md')
            return generation, seal, capture, stage
        with mock.patch.object(self.view_type, '_read_pinned', new=missing_candidate):
            observed = self.drive()
        self.assertEqual(observed[-1][0:2], ('error', 'migration_recall_parity_mismatch'), observed)

    def test_sample_search_failure_is_not_skipped(self):
        empty = SimpleNamespace(semantic_status='lexical_only', data_generation='synthetic',
                                rows=[], title_only=frozenset(), source_locations={})
        with mock.patch.object(self.v6, 'query_v6_snapshot', return_value=empty), \
             mock.patch.object(self.view_type, 'search', side_effect=RuntimeError('synthetic-search-error')):
            observed = self.drive()
        self.assertEqual(observed[-1][0], 'error', observed)

    def test_empty_candidate_search_is_not_accepted(self):
        empty = SimpleNamespace(semantic_status='lexical_only', data_generation='synthetic',
                                rows=[], title_only=frozenset(), source_locations={})
        with mock.patch.object(self.v6, 'query_v6_snapshot', return_value=empty), \
             mock.patch.object(self.view_type, 'search', return_value=[]):
            observed = self.drive()
        self.assertEqual(observed[-1][0], 'error', observed)

    def test_different_candidate_top_hit_is_not_a_parity_failure(self):
        def differing(*_args, **kwargs):
            candidate = kwargs.get('_lexical') is not None
            return SimpleNamespace(semantic_status='lexical_only', data_generation='synthetic',
                rows=[{'path': 'candidate' if candidate else 'legacy'}],
                title_only=frozenset(), source_locations={})
        with mock.patch.object(self.v6, 'query_v6_snapshot', side_effect=differing), \
             mock.patch.object(self.view_type, 'search', return_value=[{'path': 'different.md', 'score': 1.0}]):
            observed = self.drive()
        self.assertNotEqual(observed[-1][0], 'error', observed)
        self.assertEqual(self.fixture.intent(self.case)['migration_state'], 'shadow_verified', observed)


if __name__ == '__main__':
    print('python-executable=%s python-version=%s' % (sys.executable, sys.version.split()[0]), flush=True)
    unittest.main(verbosity=2)
