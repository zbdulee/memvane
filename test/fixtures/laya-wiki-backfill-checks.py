"""Initial backfill admission and terminal receipts; no models or host sessions."""
from contextlib import redirect_stdout
import argparse
import io
import hashlib
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_wiki_backfill as backfill
import laya_wiki_discovery as discovery
import wiki_compile_enqueue as enqueue
import wiki_compile_worker as worker
import wiki_selected_laya as selected
import runtime_work


class BackfillChecks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='memvane-backfill-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.wiki = self.project / '.memvane/wiki'
        self.wiki.mkdir(parents=True)
        self.journal = self.state / 'install-journal.sqlite'
        self.journal.touch()
        self.decision = {'scopeId': 'a' * 32, 'dbIncarnation': 'b' * 32,
            'queuePath': str(self.state / 'queue.sqlite'), 'projectRoot': str(self.project),
            'journalPath': str(self.journal), 'installId': 'c' * 32, 'generation': 'g-test',
            'recallSettings': {'skipPaths': ['legacy']},
            'collections': [{'name': 'docs', 'role': 'raw', 'sourceRoot': str(self.project)},
                            {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}],
            'collectionRoots': {'docs': str(self.project), 'wiki': str(self.wiki)}}
        self.config = {'indexing': True, 'collections': ['docs', 'wiki'],
            'collectionPaths': {'docs': '.', 'wiki': '.memvane/wiki'},
            'collectionRoles': {'docs': 'raw', 'wiki': 'wiki'},
            'wikiPath': '.memvane/wiki', 'skipPaths': ['legacy'],
            'compile': {'mode': 'auto-wiki', 'triggers': ['post_sync_source', 'session_start_source'],
                        'extractor': {'builtins': ['claude', 'codex', 'hermes']}}}
        self.bound = self.decision, {'productRouteAllowed': True}
        for item in (patch.object(selected, 'selected', return_value=self.bound),
                     patch.object(selected, 'recheck'),
                     patch.object(runtime_work, 'request', return_value={'status': 'queued'})):
            item.start()
            self.addCleanup(item.stop)
        self.snapshot = {}

    def add(self, rel, text='A durable fact.'):
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.snapshot['docs/' + rel] = sha
        return sha

    def start(self, live=('sync',)):
        return backfill.discover(self.decision, self.snapshot, self.config, live,
                                 {kind: 'codex' for kind in live})

    def dispatch(self, row):
        return backfill.dispatch(self.decision, row, self.snapshot, self.config, recheck=lambda: None)

    def jobs(self):
        path = self.project / '.memvane/compile/source-queue.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def settle(self, job):
        job = {**job, '_processedContentRevision': job['contentRevision']}
        backfill.settle_job(self.bound, job)

    def test_requires_selected_install_and_accepted_enabled_event(self):
        self.add('one.md')
        self.assertIsNone(self.start(()))
        self.config['compile']['triggers'] = ['manual']
        self.assertIsNone(self.start())
        self.config['compile']['triggers'] = ['post_sync_source']
        self.journal.unlink()
        self.assertIsNone(self.start())
        self.assertFalse(backfill._path(self.decision).exists())

    def test_initial_admission_excludes_controls_hidden_and_skip_paths(self):
        for rel in ('one.md', 'AGENTS.md', 'CLAUDE.md', '.nova/rule.md',
                    'legacy/old.md', 'node_modules/readme.md', 'notes.txt'):
            self.add(rel)
        row = self.start()
        self.assertEqual(list(row['items']), ['docs/one.md'])
        self.assertEqual(backfill._path(self.decision).stat().st_mode & 0o777, 0o600)

    def test_more_than_300_sources_progress_without_overflow_or_early_completion(self):
        for i in range(315):
            self.add('docs/%03d.md' % i)
        row = self.start()
        with patch.object(enqueue, 'compile_max_files', return_value=7):
            first = self.dispatch(row)
        self.assertEqual((first['pending'], first['dispatched'], first['settled']), (308, 7, 0))
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(self.dispatch(backfill._load(self.decision)), first)
        jobs = self.jobs()
        for job in jobs:
            self.settle(job)
        second = backfill._load(self.decision)
        self.assertEqual(backfill.summary(second)['settled'], 7)
        with patch.object(enqueue, 'compile_max_files', return_value=7):
            self.assertEqual(self.dispatch(second)['dispatched'], 7)
        self.assertEqual(len(self.jobs()), 14)

    def test_enqueue_then_ack_write_failure_replays_without_duplicate_source_jobs(self):
        self.add('one.md')
        row = self.start()
        real = discovery._write
        def fail_ack(path, data):
            if path == backfill._path(self.decision):
                raise OSError('write failed')
            return real(path, data)
        with patch.object(discovery, '_write', side_effect=fail_ack):
            with self.assertRaises(OSError):
                self.dispatch(row)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['pending'], 1)
        self.dispatch(backfill._load(self.decision))
        self.assertEqual(len(self.jobs()), 1)
        self.settle(self.jobs()[0])
        self.assertEqual(backfill.summary(backfill._load(self.decision))['state'], 'complete')

    def test_frozen_changed_or_deleted_revision_is_skipped_not_reauthorized(self):
        self.add('one.md')
        self.add('two.md')
        self.start()
        self.add('one.md', 'Changed bytes require an ordinary change event.')
        self.snapshot.pop('docs/two.md')
        self.add('new.md')
        row = self.start(())
        self.assertEqual(backfill.summary(row)['skipped'], 2)
        self.assertNotIn('docs/new.md', row['items'])
        self.assertEqual(self.dispatch(row)['state'], 'complete_with_skips')
        self.assertEqual(self.jobs(), [])

    def test_ordinary_job_dedup_can_settle_same_dispatched_revision(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = self.jobs()[0]
        job.pop('initialBackfill')
        self.settle(job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['settled'], 1)

    def test_wrong_binding_or_receipt_cannot_settle_work(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = self.jobs()[0]
        with self.assertRaisesRegex(discovery.DiscoveryBlocked, 'identity_changed'):
            self.settle({**job, 'initialBackfill': 'f' * 32})
        with self.assertRaisesRegex(discovery.DiscoveryBlocked, 'revision_changed'):
            self.settle({**job, 'contentRevision': 'f' * 64})
        other = {**self.decision, 'dbIncarnation': 'd' * 32}
        # An incarnation gets its own state filename and cannot acknowledge this one.
        backfill.settle_job((other, self.bound[1]), job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)

    def test_worker_does_not_extract_new_bytes_under_frozen_authority(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = self.jobs()[0]
        self.add('one.md', 'newer content')
        with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay')):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (True, False, []))
        backfill.settle_job(self.bound, job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['skipped'], 1)

    def test_originating_host_is_durable_and_ambient_engine_cannot_replace_it(self):
        self.add('one.md')
        row = self.start()
        self.assertEqual(row['engine'], 'codex')
        with patch.dict('os.environ', {'QMD_ENGINE': 'claude'}):
            self.dispatch(row)
        self.assertEqual(self.jobs()[0]['engine'], 'codex')
        resumed = backfill.discover(self.decision, self.snapshot, self.config,
                                     ('sync',), {'sync': 'claude'})
        self.assertEqual(resumed['engine'], 'codex')
        self.assertIsNone(backfill.discover({**self.decision, 'dbIncarnation': 'e' * 32},
            self.snapshot, self.config, ('sync',), {}))
        with patch.dict('os.environ', {'QMD_ENGINE': ''}):
            self.assertEqual(enqueue.compile_engine(self.config['compile']), 'unknown')

    def test_hook_grant_host_survives_provisional_activation_and_is_pruned(self):
        grant = discovery.record_grant(self.decision, 'sync', engine='codex')
        first = discovery.observe_grants(self.decision)
        self.assertEqual(first['engines'], {grant: 'codex'})
        self.assertTrue(discovery.activate_grant(self.decision, 'sync', grant))
        self.assertEqual(discovery.observe_grants(self.decision)['engines'], {grant: 'codex'})
        discovery.revoke_grant(self.decision, grant)
        self.assertNotIn('engines', discovery.observe_grants(self.decision))

    def test_settled_retry_skips_extraction_after_continuation_wake_failure(self):
        self.add('one.md')
        self.add('two.md')
        with patch.object(enqueue, 'compile_max_files', return_value=1):
            self.dispatch(self.start())
        job = self.jobs()[0]
        with patch.object(selected, 'request_sync', side_effect=OSError('wake failed')):
            with self.assertRaises(OSError):
                self.settle(job)
        with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay again')):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (True, False, []))
        backfill.settle_job(self.bound, job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['settled'], 1)

    def test_unknown_host_uses_real_attempts_once_then_records_successful_producer(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = {**self.jobs()[0], 'engine': 'unknown'}
        attempts, written = [], []
        def extract(argv, payload, timeout, root):
            attempts.append(payload['engine'])
            self.assertEqual(payload['_qmd']['reasoningEffort']['requested'], 'low')
            if payload['engine'] == 'codex':
                return None, 'extractor_failed', 127
            return {'candidates': [{'title': 'A durable fact', 'summary': 'The source states a durable fact.',
                                    'confidence': 'high'}]}, None, 0
        def compile_card(root, candidate):
            written.append(candidate)
            return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions']}
        with patch.object(worker, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'compile_candidate', side_effect=compile_card), \
             patch.object(worker, 'gather_similar_pages', return_value=[]):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (True, False, []))
        self.assertEqual(attempts, ['codex', 'claude'])
        self.assertEqual(job['engine'], 'claude')
        self.assertEqual(written[0]['engine'], 'claude')
        backfill.settle_job(self.bound, job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['settled'], 1)

    def test_known_host_failure_never_tries_a_different_cli(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = self.jobs()[0]
        with patch.object(worker, 'run_extractor', return_value=(None, 'extractor_failed', 127)) as extract, \
             patch.object(worker, 'gather_similar_pages', return_value=[]):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (False, True, []))
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(extract.call_args.args[1]['engine'], 'codex')
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)

    def test_unknown_exhaustion_is_skipped_without_rebilling_cycle(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = {**self.jobs()[0], 'engine': 'unknown'}
        with patch.object(worker, 'run_extractor', return_value=(None, 'extractor_failed', 1)) as extract, \
             patch.object(worker, 'gather_similar_pages', return_value=[]):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (True, False, []))
        self.assertEqual([call.args[1]['engine'] for call in extract.call_args_list],
                         ['codex', 'claude', 'hermes'])
        backfill.settle_job(self.bound, job)
        progress = backfill.summary(backfill._load(self.decision))
        self.assertEqual((progress['state'], progress['skipped'], progress['settled']),
                         ('complete_with_skips', 1, 0))
        with patch.object(worker, 'run_extractor', side_effect=AssertionError('must not pay again')):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (True, False, []))

    def test_unknown_resource_deferral_preserves_job_without_other_host_attempt(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = {**self.jobs()[0], 'engine': 'unknown'}
        with patch.object(worker, 'run_extractor', return_value=(None, 'resource_deferred', 75)) as extract, \
             patch.object(worker, 'gather_similar_pages', return_value=[]):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (False, True, []))
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)

    def test_budget_one_and_two_resume_only_untried_unknown_hosts(self):
        self.add('one.md')
        self.dispatch(self.start())
        for cap in (1, 2):
            with self.subTest(cap=cap):
                job = {**self.jobs()[0], 'engine': 'unknown'}
                attempted = []
                def extract(argv, payload, timeout, root):
                    attempted.append(payload['engine'])
                    if payload['engine'] == 'hermes':
                        return {'candidates': []}, None, 0
                    return None, 'extractor_failed', 127
                with patch.object(worker, 'run_extractor', side_effect=extract), \
                     patch.object(worker, 'gather_similar_pages', return_value=[]):
                    for _ in range(3):
                        budget = {'remaining': cap}
                        token = worker._EXTRACTOR_BUDGET.set(budget)
                        try:
                            result = worker.process_job(self.project, self.config,
                                self.config['compile'], job)
                        finally:
                            worker._EXTRACTOR_BUDGET.reset(token)
                        self.assertLessEqual(cap - budget['remaining'], cap)
                        if result[:2] == (True, False):
                            break
                        self.assertEqual(result[:2], (False, True))
                        self.assertTrue(budget['deferred'])
                        job = json.loads(json.dumps(job))  # durable queue round trip
                self.assertEqual(attempted, ['codex', 'claude', 'hermes'])
                self.assertEqual(job['engine'], 'hermes')
                self.assertNotIn('_fallbackAttempts', job)

    def test_actual_spawn_budget_bounds_mixed_host_jobs_and_schedules_progress(self):
        for i in range(10):
            self.add('docs/%02d.md' % i)
        self.dispatch(self.start())
        queue = self.project / '.memvane/compile/source-queue.jsonl'
        jobs = self.jobs()
        for i, job in enumerate(jobs):
            job['engine'] = 'claude' if i < 2 else 'unknown'
        queue.write_text(''.join(json.dumps(job) + '\n' for job in jobs))
        calls = []
        def extract(argv, payload, timeout, root):
            calls.append(payload['engine'])
            if len(calls) <= 2:
                return {'candidates': []}, None, 0
            return None, 'extractor_failed', 1
        out = io.StringIO()
        with patch.object(worker, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'gather_similar_pages', return_value=[]), \
             patch.object(worker, '_run_verify_pass'), \
             patch.object(selected, 'request_compile') as kick, redirect_stdout(out):
            worker._main_locked(argparse.Namespace(json=True, flush_all=True), self.project,
                self.config, self.config['compile'], queue, self.bound)
        self.assertEqual(len(calls), 10)
        report = json.loads(out.getvalue())
        self.assertTrue(report['budgetDeferred'])
        self.assertGreater(report['remaining'], 0)
        kick.assert_called_once_with(self.project, self.bound)
        receipts = [job['_fallbackAttempts'] for job in self.jobs() if '_fallbackAttempts' in job]
        self.assertTrue(receipts)
        self.assertTrue(all(len(receipt['engines']) < 3 for receipt in receipts))

    def test_resource_deferred_after_failed_host_keeps_only_failed_receipt(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = {**self.jobs()[0], 'engine': 'unknown'}
        def extract(argv, payload, timeout, root):
            return (None, 'extractor_failed', 127) if payload['engine'] == 'codex' else (None, 'resource_deferred', 75)
        with patch.object(worker, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'gather_similar_pages', return_value=[]):
            self.assertEqual(worker.process_job(self.project, self.config,
                self.config['compile'], job), (False, True, []))
        self.assertEqual(job['_fallbackAttempts']['engines'], ['codex'])

    def orphan_first_batch(self, *, markerless=False):
        self.add('one.md')
        self.add('two.md')
        with patch.object(enqueue, 'compile_max_files', return_value=1):
            self.dispatch(self.start())
        job = self.jobs()[0]
        if markerless:
            job.pop('initialBackfill')
        job['_requeue_count'] = worker.MAX_REQUEUE_COUNT
        queue = self.project / '.memvane/compile/source-queue.jsonl'
        queue.write_text('')
        orphan = queue.with_name(queue.name + '.claimed.99999999.dead')
        orphan.write_text(json.dumps(job) + '\n')
        return queue, orphan, job

    def test_orphan_retry_cap_receipt_settles_skipped_and_next_batch_proceeds(self):
        queue, orphan, job = self.orphan_first_batch()
        with patch.object(worker, '_is_pid_alive', return_value=False), \
             patch.object(worker, '_run_verify_pass'), \
             patch.object(worker, 'run_extractor', side_effect=AssertionError('terminal job must not pay')), \
             patch.object(selected, 'request_sync') as wake:
            worker._main_locked(argparse.Namespace(json=False, flush_all=True), self.project,
                self.config, self.config['compile'], queue, self.bound)
        self.assertFalse(orphan.exists())
        wake.assert_called_once_with(self.project, self.bound)
        ledger = json.loads((queue.parent / 'discard-ledger.jsonl').read_text())
        self.assertEqual(ledger['backfillReceipt']['initialBackfill'], job['initialBackfill'])
        self.assertEqual(ledger['backfillReceipt']['contentRevision'], job['contentRevision'])
        row = backfill._load(self.decision)
        self.assertEqual(backfill.summary(row)['skipped'], 1)
        self.assertEqual(backfill.summary(row)['settled'], 0)
        self.assertEqual(self.dispatch(row)['dispatched'], 1)
        self.assertEqual(self.jobs()[0]['source']['path'], 'two.md')

    def test_discard_before_ack_crash_is_reconciled_by_next_sync(self):
        queue, orphan, job = self.orphan_first_batch(markerless=True)
        with patch.object(worker, '_is_pid_alive', return_value=False):
            claimed = worker.claim_queue(queue)
        self.assertEqual(worker.read_queue(claimed), [])
        self.assertFalse(orphan.exists())
        # Simulate process death after the durable discard and claim unlink,
        # before the compile worker can acknowledge the cohort.
        row = backfill._load(self.decision)
        self.assertEqual(backfill.summary(row)['dispatched'], 1)
        self.decision['generation'] = 'g-upgraded'
        self.assertEqual(self.dispatch(row)['skipped'], 1)
        self.assertEqual(self.jobs()[0]['source']['path'], 'two.md')
        before = backfill.summary(backfill._load(self.decision))
        backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision)), before)

    def test_discard_fsync_failure_retains_orphan_and_recovery_is_idempotent(self):
        queue, orphan, _job = self.orphan_first_batch()
        with patch.object(worker, '_is_pid_alive', return_value=False), \
             patch.object(worker.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                worker.claim_queue(queue)
        self.assertTrue(orphan.exists())
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)
        with patch.object(worker, '_is_pid_alive', return_value=False):
            worker.claim_queue(queue)
        backfill.reconcile_discards(self.bound)
        row = backfill._load(self.decision)
        self.assertEqual(backfill.summary(row)['skipped'], 1)
        self.assertEqual(backfill.summary(row)['pending'], 1)

    def test_partial_atomic_receipt_write_keeps_claim_and_retry_recovers(self):
        queue, orphan, job = self.orphan_first_batch()
        row = backfill._load(self.decision)
        real_write = os.write
        calls = []
        def partial(fd, data):
            calls.append(1)
            if len(calls) == 1:
                return real_write(fd, data[:7])
            raise OSError('ENOSPC after prefix')
        with patch.object(worker, '_is_pid_alive', return_value=False), \
             patch.object(discovery.os, 'write', side_effect=partial):
            with self.assertRaises(OSError):
                worker.claim_queue(queue)
        self.assertTrue(orphan.exists())
        path = backfill._receipt_path(self.decision, row, 'docs/one.md')
        self.assertFalse(path.exists())
        with patch.object(worker, '_is_pid_alive', return_value=False):
            worker.claim_queue(queue)
        self.assertFalse(orphan.exists())
        self.assertEqual(discovery._read(path)['contentRevision'], job['contentRevision'])
        backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['skipped'], 1)

    def test_atomic_replace_failure_retains_claim(self):
        queue, orphan, _job = self.orphan_first_batch()
        with patch.object(worker, '_is_pid_alive', return_value=False), \
             patch.object(discovery.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                worker.claim_queue(queue)
        self.assertTrue(orphan.exists())
        with patch.object(worker, '_is_pid_alive', return_value=False):
            worker.claim_queue(queue)
        backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['skipped'], 1)

    def test_directory_fsync_failure_rechecks_existing_receipt_before_drop_or_ack(self):
        queue, orphan, _job = self.orphan_first_batch()
        path = backfill._receipt_path(self.decision, backfill._load(self.decision),
                                     'docs/one.md', create=True)
        real_fsync = os.fsync
        def fail_receipt_directory(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == path.parent.stat().st_ino:
                raise OSError('receipt directory fsync failed')
            return real_fsync(fd)
        with patch.object(worker, '_is_pid_alive', return_value=False), \
             patch.object(discovery.os, 'fsync', side_effect=fail_receipt_directory):
            for _retry in range(2):
                with self.assertRaises(OSError):
                    worker.claim_queue(queue)
                self.assertTrue(orphan.exists())
                self.assertTrue(path.exists())
            # A crash after replace cannot make reconciliation skip the same
            # durability barrier, even though the JSON is already readable.
            with self.assertRaises(OSError):
                backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)
        with patch.object(worker, '_is_pid_alive', return_value=False):
            worker.claim_queue(queue)
        backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['skipped'], 1)

    def test_torn_audit_append_is_not_canonical_backfill_evidence(self):
        queue, orphan, _job = self.orphan_first_batch()
        def torn_audit(path, payload):
            with path.open('ab') as handle:
                handle.write(b'{"timestamp":')
            raise OSError('audit ENOSPC')
        with patch.object(worker, '_is_pid_alive', return_value=False), \
             patch.object(worker, 'append_jsonl', side_effect=torn_audit):
            with self.assertRaises(OSError):
                worker.claim_queue(queue)
        self.assertTrue(orphan.exists())
        with patch.object(worker, '_is_pid_alive', return_value=False):
            worker.claim_queue(queue)
        self.assertFalse(orphan.exists())
        # Concatenated audit JSON is intentionally ignored by reconciliation.
        backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['skipped'], 1)

    def test_invalid_or_symlink_atomic_receipt_fails_closed(self):
        queue, orphan, job = self.orphan_first_batch()
        with patch.object(worker, '_is_pid_alive', return_value=False):
            worker.claim_queue(queue)
        row = backfill._load(self.decision)
        path = backfill._receipt_path(self.decision, row, 'docs/one.md')
        receipt = discovery._read(path)
        discovery._write(path, {**receipt, 'initialBackfill': 'f' * 32})
        with self.assertRaises(discovery.DiscoveryBlocked):
            backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)
        external = self.root / 'foreign.json'
        discovery._write(external, receipt)
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(discovery.DiscoveryBlocked):
            backfill.reconcile_discards(self.bound)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)

    def test_rebound_route_and_fallback_receipt_are_persisted_before_budget_retry(self):
        self.add('one.md')
        self.dispatch(self.start())
        queue = self.project / '.memvane/compile/source-queue.jsonl'
        job = self.jobs()[0]
        job['engine'] = 'unknown'
        queue.write_text(json.dumps(job) + '\n')
        self.config['compile']['budget'] = {'extractorPerRun': 1}
        attempted = []
        def extract(_argv, payload, _timeout, _root):
            attempted.append(payload['engine'])
            if payload['engine'] != 'hermes':
                return None, 'extractor_failed', 127
            return {'candidates': []}, None, 0
        with patch.object(worker, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'gather_similar_pages', return_value=[]), \
             patch.object(worker, '_run_verify_pass'), patch.object(selected, 'request_compile'):
            args = argparse.Namespace(json=False, flush_all=True)
            worker._main_locked(args, self.project, self.config, self.config['compile'], queue, self.bound)
            self.assertEqual(self.jobs()[0]['_fallbackAttempts']['engines'], ['codex'])
            self.decision['generation'] = 'g-upgraded'
            worker._main_locked(args, self.project, self.config, self.config['compile'], queue, self.bound)
            persisted = self.jobs()[0]
            self.assertEqual(persisted['selectedRoute'], selected.route_identity(self.bound))
            self.assertEqual(persisted['_fallbackAttempts']['selectedRoute'], persisted['selectedRoute'])
            self.assertEqual(persisted['_fallbackAttempts']['engines'], ['codex', 'claude'])
            worker._main_locked(args, self.project, self.config, self.config['compile'], queue, self.bound)
        self.assertEqual(attempted, ['codex', 'claude', 'hermes'])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(backfill.summary(backfill._load(self.decision))['settled'], 1)


if __name__ == '__main__':
    unittest.main()
