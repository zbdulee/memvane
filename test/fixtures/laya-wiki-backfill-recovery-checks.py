"""Generation-only source-job recovery against real frozen-cohort authority.

All source files and queues are synthetic. Extractors are replaced before worker
entry, so a regression cannot invoke a host CLI or a model.
"""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


_path = Path(__file__).with_name('laya-wiki-backfill-checks.py')
_spec = importlib.util.spec_from_file_location('backfill_fixture_helpers', _path)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
backfill, worker, selected = _fixture.backfill, _fixture.worker, _fixture.selected


class GenerationRecoveryChecks(unittest.TestCase):
    # Reuse setup/helpers without inheriting and rerunning the original cases.
    setUp = _fixture.BackfillChecks.setUp
    add = _fixture.BackfillChecks.add
    start = _fixture.BackfillChecks.start
    dispatch = _fixture.BackfillChecks.dispatch
    jobs = _fixture.BackfillChecks.jobs

    def dispatched(self, *, marker=True, engine='codex'):
        self.add('one.md')
        self.dispatch(self.start())
        job = self.jobs()[0]
        job['engine'] = engine
        if not marker:
            job.pop('initialBackfill')
        return job

    def process(self, job):
        return worker.process_job(self.project, self.config, self.config['compile'], job)

    def test_markerless_dispatched_revision_rebinds_and_settles_after_bundle_change(self):
        job = self.dispatched(marker=False)
        old_route = dict(job['selectedRoute'])
        self.decision['generation'] = 'g-new-bundle'
        attempts = []
        def extract(_argv, payload, _timeout, _root):
            attempts.append(payload['engine'])
            return {'candidates': []}, None, 0
        with patch.object(worker, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'gather_similar_pages', return_value=[]):
            self.assertEqual(self.process(job), (True, False, []))
        self.assertEqual(attempts, ['codex'])
        self.assertNotEqual(old_route, job['selectedRoute'])
        self.assertEqual(job['selectedRoute'], selected.route_identity(self.bound))
        self.assertEqual(job['contentRevision'], self.snapshot['docs/one.md'])
        backfill.settle_job(self.bound, job)
        progress = backfill.summary(backfill._load(self.decision))
        self.assertEqual((progress['state'], progress['settled'], progress['dispatched']),
                         ('complete', 1, 0))

    def test_unknown_fallback_receipt_resumes_claude_without_rebilling_codex(self):
        job = self.dispatched(engine='unknown')
        attempts, written = [], []
        def extract(_argv, payload, _timeout, _root):
            attempts.append(payload['engine'])
            if payload['engine'] == 'codex':
                return None, 'extractor_failed', 127
            return {'candidates': [{'title': 'A durable fact',
                'summary': 'The source states a durable fact.', 'confidence': 'high'}]}, None, 0
        def compile_card(_root, candidate):
            written.append(candidate)
            return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions']}
        with patch.object(worker, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'compile_candidate', side_effect=compile_card), \
             patch.object(worker, 'gather_similar_pages', return_value=[]):
            token = worker._EXTRACTOR_BUDGET.set({'remaining': 1})
            try:
                self.assertEqual(self.process(job), (False, True, []))
            finally:
                worker._EXTRACTOR_BUDGET.reset(token)
            self.assertEqual(job['_fallbackAttempts']['engines'], ['codex'])
            job = json.loads(json.dumps(job))  # Persist/reload between runs.
            self.decision['generation'] = 'g-new-bundle'
            token = worker._EXTRACTOR_BUDGET.set({'remaining': 1})
            try:
                self.assertEqual(self.process(job), (True, False, []))
            finally:
                worker._EXTRACTOR_BUDGET.reset(token)
        self.assertEqual(attempts, ['codex', 'claude'])
        self.assertEqual(job['engine'], 'claude')
        self.assertNotIn('_fallbackAttempts', job)
        self.assertEqual(written[0]['engine'], 'claude')
        self.assertEqual(job['selectedRoute'], selected.route_identity(self.bound))
        backfill.settle_job(self.bound, job)
        progress = backfill.summary(backfill._load(self.decision))
        self.assertEqual((progress['settled'], progress['dispatched']), (1, 0))

    def test_changed_install_scope_or_incarnation_never_rebinds_or_calls_extractor(self):
        original = self.dispatched(marker=False)
        self.decision['generation'] = 'g-new-bundle'
        for field in ('installId', 'scopeId', 'dbIncarnation'):
            with self.subTest(field=field):
                job = json.loads(json.dumps(original))
                job['selectedRoute'][field] = 'f' * 32
                with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay')):
                    self.assertEqual(self.process(job), (False, True, []))
                self.assertNotIn('initialBackfill', job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)

    def test_unrelated_markerless_job_has_no_rebind_authority(self):
        job = self.dispatched(marker=False)
        self.decision['generation'] = 'g-new-bundle'
        other_hash = self.add('unrelated.md')
        job['source']['path'] = 'unrelated.md'
        job['contentRevision'] = other_hash
        with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay')):
            self.assertEqual(self.process(job), (False, True, []))
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)

    def test_markerless_pending_revision_cannot_claim_a_dispatched_job(self):
        self.add('one.md')
        other_hash = self.add('two.md')
        with patch.object(_fixture.enqueue, 'compile_max_files', return_value=1):
            self.dispatch(self.start())
        job = self.jobs()[0]
        job.pop('initialBackfill')
        job['source']['path'] = 'two.md'
        job['contentRevision'] = other_hash
        self.decision['generation'] = 'g-new-bundle'
        with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay')):
            self.assertEqual(self.process(job), (False, True, []))
        progress = backfill.summary(backfill._load(self.decision))
        self.assertEqual((progress['pending'], progress['dispatched']), (1, 1))

    def test_tampered_job_hash_refuses_but_changed_source_is_terminally_skipped(self):
        original = self.dispatched(marker=False)
        self.decision['generation'] = 'g-new-bundle'
        tampered = json.loads(json.dumps(original))
        tampered['contentRevision'] = 'f' * 64
        with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay')):
            self.assertEqual(self.process(tampered), (False, True, []))
        self.add('one.md', 'New bytes need their own source-event authority.')
        with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay')):
            self.assertEqual(self.process(original), (True, False, []))
        self.assertTrue(original['_backfillSkipped'])
        backfill.settle_job(self.bound, original)
        progress = backfill.summary(backfill._load(self.decision))
        self.assertEqual((progress['state'], progress['skipped'], progress['dispatched']),
                         ('complete_with_skips', 1, 0))


if __name__ == '__main__':
    unittest.main()
