"""Compile fingerprints describe the same source read and checkout observation."""
import hashlib
import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from memvane.store import api as store
from memvane.wiki.card import api as card
from memvane.wiki.compile import wiki_compile as compiler, wiki_compile_worker as worker
from memvane.wiki.engines import api as engines
from memvane.wiki.intake import api as intake
from memvane.wiki.verify import api as verify
from memvane.wiki.workspace import api as workspace
from tests.support.queue_files import pending_text, replace_queue
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class CheckoutFingerprints(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.source = self.project.write_note('docs/source.md', 'synthetic source\n')
        self.project.git('add', '--', '.memvane/settings.json')
        self.project.git('commit', '-qm', 'fixture')
        self.config = expanded(self.project, compile={'mode': 'auto-wiki',
                               'extractor': {'builtins': ['claude']}})
        use_config(self, self.project, self.config)
        decision = {'state': 'candidate', 'projectRoot': str(self.project.root),
                    'installId': 'a' * 32, 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                    'recallSettings': {'skipPaths': []}}
        self.bound = (decision, {'productRouteAllowed': True})
        binding = store.observe_binding(self.tmp / 'checkout.sqlite', scope_id='b' * 32,
                                        create_root=str(self.project.root))
        repo, worktree = binding['repository'], binding['worktree']
        self.checkout = SimpleNamespace(
            capture=lambda: store.capture_view(str(self.project.root), repo, worktree, fresh=True),
            matches=lambda capture: store.capture_matches(capture, repo, worktree, fresh=True))
        self.calls, self.candidates = [], []
        def extract(argv, payload, timeout, root):
            self.calls.append(payload)
            return {'candidates': [{'title': 'Source', 'summary': 'Synthetic fact',
                                    'sourceFingerprints': {'schema': 'model-decoy'}}]}, None, 0
        def compile_candidate(root, candidate, **kwargs):
            self.candidates.append(candidate)
            return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions']}
        for item in (patch.object(workspace, 'selected', return_value=self.bound),
                     patch.object(workspace, 'recheck'), patch.object(engines, 'run_extractor', side_effect=extract),
                     patch.object(worker, 'compile_candidate', side_effect=compile_candidate),
                     patch.object(worker, 'gather_similar_pages', return_value=[])):
            item.start()
            self.addCleanup(item.stop)

    def job(self):
        return {'trigger': 'post_tool_source', 'engine': 'claude', 'cwd': str(self.project.root),
                'selectedRoute': workspace.route_identity(self.bound),
                'source': {'kind': 'file', 'path': 'docs/source.md', 'collection': self.project.raw_name}}

    def process(self, checkout=None):
        return worker.process_job(self.project.root, self.config, self.config['compile'], self.job(),
                                  checkout=self.checkout if checkout is None else checkout)

    def test_untracked_staged_committed_and_detached_sources_are_observed(self):
        for state in ('untracked', 'staged', 'committed', 'detached'):
            if state == 'staged':
                self.project.git('add', '--', 'docs/source.md')
            elif state == 'committed':
                self.project.git('commit', '-qm', 'source')
            elif state == 'detached':
                self.project.git('checkout', '-q', '--detach')
            with self.subTest(state=state):
                self.assertEqual(self.process(), (True, False, []))
                candidate = self.candidates[-1]
                spec = candidate['sourceFingerprints']
                row, revision = spec['fingerprints'][0], candidate['sourceRevisions'][0]
                self.assertEqual((row['path'], row['source_sha'], row['byte_length']),
                                 (revision['path'], revision['sha256'], revision['size']))
                self.assertEqual(row['source_sha'], hashlib.sha256(self.source.read_bytes()).hexdigest())
                self.assertEqual(row['tracked_state'], 'untracked' if state == 'untracked' else 'tracked')
                self.assertEqual(row['binding_state'], 'observed')
                self.assertEqual(row['branch_key'].startswith('detached:'), state == 'detached')
                self.assertNotEqual(spec['schema'], 'model-decoy')
        self.assertEqual(len(self.calls), 4)

    def test_changed_checkout_during_source_read_never_calls_extractor(self):
        original = card.snapshot_bytes
        for action in (('switch', '-qc', 'other-branch'),
                       ('commit', '-qm', 'head-change', '--allow-empty'),
                       ('add', '--', 'docs/source.md')):
            def snapshot(path):
                result = original(path)
                self.project.git(*action)
                return result
            with self.subTest(action=action[0]), patch.object(card, 'snapshot_bytes', side_effect=snapshot):
                self.assertEqual(self.process(), (False, True, []))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.candidates, [])

    def test_unobservable_checkout_preserves_job(self):
        def unavailable():
            raise OSError('synthetic checkout unavailable')
        checkout = SimpleNamespace(capture=unavailable, matches=lambda value: False)
        self.assertEqual(self.process(checkout), (False, True, []))
        self.assertEqual(self.calls, [])

    def test_git_tracking_failure_stays_unknown_but_source_is_compiled(self):
        from memvane.wiki.lifecycle import api as lifecycle
        with patch.object(lifecycle, 'tracked_states', return_value={'docs/source.md': 'unknown'}):
            self.assertEqual(self.process(), (True, False, []))
        row = self.candidates[0]['sourceFingerprints']['fingerprints'][0]
        self.assertEqual(row['tracked_state'], 'unknown')
        self.assertEqual(len(self.calls), 1)

    def test_scan_id_changes_per_observation_of_the_same_bytes(self):
        self.assertEqual(self.process(), (True, False, []))
        self.assertEqual(self.process(), (True, False, []))
        first, second = [candidate['sourceFingerprints']['fingerprints'][0]
                         for candidate in self.candidates]
        self.assertNotEqual(first['scan_id'], second['scan_id'])
        self.assertEqual(first['source_sha'], second['source_sha'])

    def test_model_fingerprints_are_dropped_without_checkout(self):
        self.assertEqual(worker.process_job(self.project.root, self.config, self.config['compile'],
                                            self.job(), checkout=None), (True, False, []))
        self.assertNotIn('sourceFingerprints', self.candidates[0])

    def test_batch_supplies_selected_checkout_to_real_job(self):
        queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True, exist_ok=True)
        replace_queue(queue, json.dumps(self.job()) + '\n')
        with patch.object(workspace, 'schema3_checkout', return_value=self.checkout), \
             patch.object(intake, 'reconcile_discards'), patch.object(intake, 'settle_job'), \
             patch.object(worker, 'recover_pending_refreshes'), patch.object(worker, '_run_verify_pass'):
            self.assertEqual(worker._main_locked(SimpleNamespace(json=False, flush_all=True),
                             self.project.root, self.config, self.config['compile'], queue, self.bound), 0)
        row = self.candidates[0]['sourceFingerprints']['fingerprints'][0]
        self.assertEqual((row['path'], row['source_sha']),
                         ('docs/source.md', self.candidates[0]['sourceRevisions'][0]['sha256']))

    def test_batch_reader_error_preserves_queue_without_extraction(self):
        queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True, exist_ok=True)
        replace_queue(queue, json.dumps(self.job()) + '\n')
        with patch.object(workspace, 'schema3_checkout', side_effect=sqlite3.OperationalError('fixture busy')), \
             patch.object(intake, 'reconcile_discards'), patch.object(worker, 'recover_pending_refreshes'), \
             patch.object(worker, '_run_verify_pass'):
            self.assertEqual(worker._main_locked(SimpleNamespace(json=False, flush_all=True),
                             self.project.root, self.config, self.config['compile'], queue, self.bound), 0)
        self.assertEqual(self.calls, [])
        self.assertTrue(pending_text(queue).strip())
        audit = self.project.root / '.memvane/compile/candidates.jsonl'
        self.assertEqual(json.loads(audit.read_text().splitlines()[-1])['reason'], 'checkout_unavailable')

    def test_writer_places_observed_fingerprint_in_verify_job(self):
        self.assertEqual(self.process(), (True, False, []))
        candidate = self.candidates[0]
        with patch.object(workspace, 'request_sync') as request_sync, patch.object(workspace, 'append_activity'), \
             patch.object(compiler, 'judge_new_page_duplicate',
                          return_value=(None, None, {'outcome': 'unavailable', 'reason': 'fixture'})), \
             patch.object(compiler, 'find_wiki_semantic_match', return_value=(None, None)), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(compiler._compile_locked(self.project.root, self.config,
                             self.config['compile'], self.project.wiki, candidate), 0)
        request_sync.assert_called_once_with(self.project.root, self.bound)
        from tests.support.wiki_handoff import verify_rows
        row, = verify_rows(self.project.root)
        job = row['payload']
        self.assertEqual((job['kind'], row['image']), ('review', None))
        observed, stored = candidate['sourceFingerprints'], job['sourceFingerprints']
        self.assertEqual(job['engine'], 'claude')
        self.assertRegex(job['sourceHash'], r'^[a-f0-9]{16}$')
        self.assertNotIn('authoritativeSources', (self.project.root / job['targetPath']).read_text())
        self.assertEqual(stored['viewId'], observed['viewId'])
        self.assertEqual(stored['fingerprints'], observed['fingerprints'])
        self.assertEqual(stored['scanId'], observed['fingerprints'][0]['scan_id'])
        self.assertEqual(stored['observedNs'], observed['fingerprints'][0]['observed_ns'])
        page = self.project.root / job['targetPath']
        def trust_state():
            block = compiler.FRONTMATTER_RE.match(page.read_text()).group(1)
            values = card.parse_frontmatter_scalars(block)
            revisions = card.frontmatter_source_revisions(block)
            meta = {'status': values.get('status'), 'createdBy': values.get('createdBy'),
                    'sourceRevisions': revisions}
            return card.is_auto_trusted_card(meta), card.check_card(meta, self.project.root,
                                                                     card.allow_roots_of(self.config))
        self.assertFalse(trust_state()[0])
        self.assertTrue(verify.stamp_verification(page, 'verified', 'claude', 'self',
                                                  card.card_body_hash(page.read_text())))
        trusted, freshness = trust_state()
        self.assertTrue(trusted)
        self.assertEqual(freshness['state'], 'fresh')
        self.source.write_text('different source content\n')
        self.assertEqual(trust_state()[1]['state'], 'stale')

    def test_model_cannot_replace_queue_authority_or_compiler_owned_revision(self):
        forged = {'title': 'Source', 'summary': 'Synthetic fact', 'engine': 'decoy',
                  'trigger': 'explicit_user_approval', 'sourceRevisions': [{'path': 'decoy'}],
                  'authoritativeSources': [{'kind': 'file', 'path': 'decoy'}],
                  'sources': [{'kind': 'file', 'path': 'docs/decoy.md'},
                              {'kind': 'file', 'path': 'docs/source.md', 'collection': 'decoy'}]}
        absent = {'title': 'Absent', 'summary': 'Synthetic fact'}
        decoy = {'title': 'Decoy', 'summary': 'Synthetic fact', 'sourceRevisions': [
            {'kind': 'file', 'path': 'decoy.md', 'collection': 'raw',
             'sha256': '0' * 64, 'size': 1, 'mtimeNs': 1}]}
        with patch.object(engines, 'run_extractor', return_value=({'candidates': [absent, decoy, forged]}, None, 0)):
            self.assertEqual(self.process(), (True, False, []))
        candidate = self.candidates[-1]
        self.assertEqual((candidate['engine'], candidate['trigger']), ('claude', 'post_tool_source'))
        self.assertEqual([item['path'] for item in candidate['sources']],
                         ['docs/source.md', 'docs/decoy.md'])
        self.assertEqual([item['path'] for item in candidate['authoritativeSources']], ['docs/source.md'])
        self.assertEqual(candidate['sourceRevisions'][0]['sha256'],
                         hashlib.sha256(self.source.read_bytes()).hexdigest())
        for item in self.candidates:
            self.assertEqual(item['sourceRevisions'][0]['path'], 'docs/source.md')
            self.assertEqual(item['sourceRevisions'][0]['sha256'],
                             hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_source_change_during_extraction_preserves_job_without_writing(self):
        def extract(argv, payload, timeout, root):
            self.source.write_text('changed during extraction\n')
            return {'candidates': [{'title': 'Source', 'summary': 'Synthetic fact'}]}, None, 0
        with patch.object(engines, 'run_extractor', side_effect=extract):
            processed, preserve, _ = self.process()
        self.assertFalse(processed)
        self.assertTrue(preserve)
        self.assertEqual(self.candidates, [])

    def test_permanent_invalid_output_and_transient_errors_have_distinct_queue_policy(self):
        cases = [('invalid_extractor_json', 1, (True, False), False),
                 ('extractor_failed', 1, (False, True), True),
                 ('extractor_unavailable', 127, (False, True), False),
                 ('resource_deferred', 1, (False, True), False)]
        for reason, exit_code, expected, cooled in cases:
            with self.subTest(reason=reason):
                worker.cooldown_path(self.project.root).unlink(missing_ok=True)
                with patch.object(engines, 'run_extractor', return_value=(None, reason, exit_code)):
                    result = self.process()
                self.assertEqual(result[:2], expected)
                self.assertEqual(worker.cooldown_active(self.project.root), cooled)
                self.assertEqual(self.candidates, [])
        audit = self.project.root / '.memvane/compile/candidates.jsonl'
        self.assertNotIn('synthetic source\n', audit.read_text())

    def test_unsafe_or_non_source_paths_are_rejected_before_extractor(self):
        self.project.write_note('.hidden/secret.md', 'secret\n')
        self.project.write_note('docs/script.py', 'print(1)\n')
        for rel in ('../outside.md', '.hidden/secret.md', 'docs/script.py'):
            with self.subTest(path=rel):
                job = self.job()
                job['source']['path'] = rel
                result = worker.process_job(self.project.root, self.config, self.config['compile'], job)
                self.assertEqual(result[:2], (True, False))
        for collection in ('missing-collection', self.project.wiki_name):
            with self.subTest(collection=collection):
                job = self.job()
                job['source']['collection'] = collection
                self.assertEqual(worker.process_job(self.project.root, self.config, self.config['compile'], job)[:2],
                                 (True, False))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.candidates, [])

    def test_extractor_receives_bundled_rules_not_project_root_pages(self):
        for name in ('SCHEMA.md', 'index.md', 'log.md'):
            (self.project.wiki / name).write_text('PRIVATE_ROOT_SENTINEL')
        self.assertEqual(self.process(), (True, False, []))
        first = self.calls[-1]['wiki']
        self.assertEqual(first['schema'], worker.card_rules('default'))
        self.assertNotIn('PRIVATE_ROOT_SENTINEL', repr(first))
        self.assertNotIn('index', first)
        (self.project.root / '04_Manuscript').mkdir()
        self.assertEqual(self.process(), (True, False, []))
        second = self.calls[-1]['wiki']
        self.assertEqual(second['schema'], worker.card_rules('novel'))
        self.assertNotEqual(second['schema'], first['schema'])
        self.assertNotIn('PRIVATE_ROOT_SENTINEL', repr(second))

    def test_worker_binds_latest_pending_event_and_preserves_job_if_resolution_fails(self):
        old = card.record_pending_refresh(self.project.root, 'docs/source.md', 'claude')
        latest = card.record_pending_refresh(self.project.root, 'docs/source.md', 'codex')
        job = self.job()
        job['pendingRefresh'] = {key: old[key] for key in ('eventId', 'ts', 'engine')}
        def write(root, candidate):
            self.candidates.append(candidate)
            return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions'],
                    'sourceRefreshEventId': candidate['sourceRefreshEventId']}
        with patch.object(card, 'resolve_pending_refresh', return_value=False) as resolve, \
             patch.object(worker, 'compile_candidate', side_effect=write):
            result = worker.process_job(self.project.root, self.config, self.config['compile'], job,
                                        checkout=self.checkout)
        self.assertEqual(result[:2], (True, True))
        self.assertEqual(self.candidates[-1]['sourceRefreshEventId'], latest['eventId'])
        self.assertEqual(resolve.call_args.args[1]['eventId'], latest['eventId'])

    def test_worker_uses_one_snapshot_despite_aba_source_change(self):
        original = self.source.read_text()
        transient = 'different transient source\n'
        real_snapshot = card.snapshot_bytes
        changed = []
        def snapshot(path):
            result = real_snapshot(path)
            if path == self.source and not changed:
                self.source.write_text(transient)
                changed.append(True)
            return result
        skipped = self.project.root / '.memvane/compile/verify-skipped.jsonl'
        skipped.parent.mkdir(parents=True, exist_ok=True)
        skipped.write_text(json.dumps({'sourcePath': 'docs/source.md',
                                       'sourceBodyHash': verify.source_body_hash(transient)}) + '\n')
        payloads = []
        def extract(argv, payload, timeout, root):
            payloads.append(payload)
            self.source.write_text(original)
            return {'candidates': [{'title': 'Source', 'summary': 'Synthetic fact'}]}, None, 0
        with patch.object(card, 'snapshot_bytes', side_effect=snapshot), \
             patch.object(engines, 'run_extractor', side_effect=extract):
            self.assertEqual(self.process()[:2], (True, False))
        self.assertEqual(payloads[0]['source']['content'], original)
        self.assertEqual(self.candidates[0]['sourceRevisions'][0]['sha256'],
                         hashlib.sha256(original.encode()).hexdigest())

    def test_writer_rejection_preserves_job_and_active_cooldown_skips_extractor(self):
        job = self.job()
        with patch.object(worker, 'compile_candidate', return_value={'action': 'rejected'}):
            self.assertEqual(worker.process_job(self.project.root, self.config, self.config['compile'], job)[:2],
                             (False, True))
        self.assertTrue(job['partialCompile'])
        self.calls.clear()
        worker.set_cooldown(self.project.root, 600)
        self.assertEqual(self.process()[:2], (False, True))
        self.assertEqual(self.calls, [])

    def test_text_source_is_accepted_and_empty_similar_pages_are_omitted(self):
        self.project.write_note('docs/note.txt', 'A synthetic text source.\n')
        job = self.job()
        job['source']['path'] = 'docs/note.txt'
        self.assertEqual(worker.process_job(self.project.root, self.config, self.config['compile'], job)[:2],
                         (True, False))
        self.assertNotIn('similarPages', self.calls[-1]['wiki'])
        self.assertEqual(self.calls[-1]['source']['path'], 'docs/note.txt')

    def test_truncated_model_input_keeps_full_file_revision_hash(self):
        self.source.write_text('synthetic-' * 2000)
        raw = self.source.read_bytes()
        self.assertEqual(self.process()[:2], (True, False))
        payload = self.calls[-1]['source']
        self.assertTrue(payload['truncated'])
        self.assertEqual(len(payload['content']), 12000)
        self.assertEqual(self.candidates[-1]['sourceRevisions'][0]['sha256'], hashlib.sha256(raw).hexdigest())

    def test_worker_exception_restores_the_claimed_job(self):
        queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True, exist_ok=True)
        replace_queue(queue, json.dumps(self.job()) + '\n')
        with patch.object(workspace, 'schema3_checkout', return_value=self.checkout), \
             patch.object(intake, 'reconcile_discards'), patch.object(worker, 'recover_pending_refreshes'), \
             patch.object(worker, 'process_job', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                worker._main_locked(SimpleNamespace(json=False, flush_all=True), self.project.root,
                                    self.config, self.config['compile'], queue, self.bound)
        self.assertEqual(json.loads(pending_text(queue))['source']['path'], 'docs/source.md')

    def test_failed_requeue_preserves_claim_for_later_recovery(self):
        queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True, exist_ok=True)
        replace_queue(queue, json.dumps(self.job()) + '\n')
        with patch.object(workspace, 'schema3_checkout', return_value=self.checkout), \
             patch.object(intake, 'reconcile_discards'), patch.object(worker, 'recover_pending_refreshes'), \
             patch.object(worker, 'process_job', return_value=(False, True, [])), \
             patch.object(worker, '_commit_source_claim', side_effect=OSError('synthetic full disk')):
            with self.assertRaisesRegex(OSError, 'synthetic full disk'):
                worker._main_locked(SimpleNamespace(json=False, flush_all=True), self.project.root,
                                    self.config, self.config['compile'], queue, self.bound)
        from tests.support.wiki_handoff import claimed_jobs
        claim, = claimed_jobs(queue)
        self.assertEqual(claim['source']['path'], 'docs/source.md')

    def test_effort_audit_follows_actual_adapter_capability(self):
        self.config['compile']['reasoningEffort']['generation'] = 'high'
        builtin = engines.builtin_adapter_argv('claude')
        answer = {'candidates': [{'title': 'Source', 'summary': 'Synthetic fact'}],
                  '_qmd': {'reasoningEffort': {'requested': 'high', 'applied': 'high',
                                                'status': 'applied', 'reason': 'capability_flag'}}}
        for argv, expected in ((['python3', 'custom.py'], 'unsupported'), (builtin, 'applied')):
            with self.subTest(argv=argv), patch.object(worker, 'resolve_extractor_argv', return_value=argv), \
                 patch.object(engines, 'run_extractor', return_value=(answer, None, 0)):
                self.assertEqual(self.process()[:2], (True, False))
                effort = self.candidates[-1]['_qmd']['reasoningEffort']
                self.assertEqual(effort['status'], expected)
                self.assertEqual(effort['applied'], 'high' if expected == 'applied' else None)

    def test_worker_reports_debounce_delay_and_flush_processes_the_same_job(self):
        queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True, exist_ok=True)
        job = self.job()
        job['ts'] = datetime.now(timezone.utc).isoformat()
        replace_queue(queue, json.dumps(job) + '\n')
        self.config['compile']['batch'] = {'idleSeconds': 90, 'maxItems': 5}
        with patch.object(workspace, 'schema3_checkout', return_value=self.checkout), \
             patch.object(intake, 'reconcile_discards'), patch.object(intake, 'settle_job'), \
             patch.object(worker, 'recover_pending_refreshes'), patch.object(worker, '_run_verify_pass'), \
             patch.object(worker, 'process_job', return_value=(True, False, [])) as process:
            output = io.StringIO()
            with redirect_stdout(output):
                worker._main_locked(SimpleNamespace(json=True, flush_all=False), self.project.root,
                                    self.config, self.config['compile'], queue, self.bound)
            report = json.loads(output.getvalue())
            self.assertEqual((report['processed'], report['remaining']), (0, 1))
            self.assertGreaterEqual(report['wakeAfterSeconds'], 1)
            self.assertLessEqual(report['wakeAfterSeconds'], 90)
            process.assert_not_called()
            output = io.StringIO()
            with redirect_stdout(output):
                worker._main_locked(SimpleNamespace(json=True, flush_all=True), self.project.root,
                                    self.config, self.config['compile'], queue, self.bound)
            report = json.loads(output.getvalue())
            self.assertEqual((report['processed'], report['remaining']), (1, 0))
            process.assert_called_once()

    def test_non_git_and_unborn_checkout_have_no_head_and_untracked_source(self):
        for case in ('non-git', 'unborn'):
            with self.subTest(case=case):
                project = make_project(self.home, name=case, dirname=case, git=False)
                source = project.write_note('docs/note.md', 'synthetic source\n')
                if case == 'unborn':
                    project.git('init', '-q', '-b', 'main')
                binding = store.observe_binding(self.tmp / (case + '.sqlite'), scope_id='d' * 32,
                                                create_root=str(project.root))
                repo, worktree = binding['repository'], binding['worktree']
                capture = store.capture_view(str(project.root), repo, worktree, fresh=True)
                checkout = SimpleNamespace(matches=lambda value: store.capture_matches(value, repo, worktree,
                                                                                       fresh=True))
                revision = {'path': 'docs/note.md', **card.snapshot_bytes(source)[0]}
                spec = worker.source_fingerprints(project.root, checkout, capture, [revision], 1)
                row = spec['fingerprints'][0]
                self.assertEqual((row['head_oid'], row['tracked_state']), (None, 'untracked'))
                self.assertEqual(row['branch_key'], 'non-git' if case == 'non-git' else 'refs/heads/main')
