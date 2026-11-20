"""1.0.21 W/L: the default-teacher worker pass and the prequential learning path.

Every pass goes through the worker entry (``project_worker.run``). Fake host
CLIs answer on the child ``PATH``; the trainer, the inference transport and
the grant are fixtures. The shared DB, learning and evaluation queues, staged
cycles, candidate application and the namespace files are real.
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

from memvane.learning import default_enrollment
from memvane.learning.context_learning import (automatic_staging, candidate_apply, general_teacher,
                                               inference_identity, learning_profile, prequential,
                                               project_scope, project_worker, prompt_retirement,
                                               runtime_authorization, shared)
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture, edit_sample, prompt_sample

VIEW = 'tool-edit-8192-choice-v1'


class FakeTrainer:
    """Writes a candidate artifact; predicts the gold or nothing per artifact."""

    def __init__(self, case):
        self.case = case

    def call(self, mode, request, result_path, *, cwd):
        if mode == 'train':
            self.case.trained.append(sorted(c['request_id'].split(':', 1)[1] for c in request['train']))
            path = request['artifact_path']
            with open(path, 'wb') as out:
                out.write(b'candidate-' + request['manifest_sha256'].encode())
            os.chmod(path, 0o600)
            return {'schema_version': 1, 'status': 'trained'}, {'fake': True}
        assert mode == 'predict', mode
        base = request['artifact_path'] == str(self.case.base)
        style = self.case.base_style if base else self.case.candidate_style
        self.case.predicted.append(len(request['cases']))
        return {'predictions': [{'request_id': c['request_id'], 'input_sha256': c['input_sha256'],
                                 'selected_ids': self.case.gold_ids[c['request_id']] if style == 'gold' else []}
                                for c in request['cases']]}, {'fake': True}


class WorkerFixture(TeacherFixture):
    def setUp(self):
        super().setUp()
        self.kind = 'default-teacher'
        self.trained, self.predicted, self.recorded = [], [], []
        self.gold_ids, self.selections = {}, {}
        self.base_style, self.candidate_style = 'empty', 'gold'
        self.cohort = {'schema': project_scope.COHORT_SCHEMA, 'id': 'default-v1', 'notBeforeUnixMs': 0,
                       'minTrainCases': 5, 'minQualityTrainCases': 100, 'minTrainFamilies': 5,
                       'minValidationCases': 50, 'minValidationFamilies': 5, 'minEmptyValidation': 10}
        self.grant = {'scope': project_scope.SCOPE, 'projectRoot': str(self.project),
                      'namespaceRoot': str(self.root),
                      'operations': list(project_scope.DEFAULT_TEACHER_OPERATIONS),
                      'cohort': self.cohort}
        self.base = self.root / 'base.artifact'
        self.base.write_bytes(b'base')
        self.base.chmod(0o600)
        model = self.root / 'model'
        model.mkdir(mode=0o700)
        self.shared_policy = {'schema': shared.SCHEMA, 'enabled': True, '_runtime_authorization_v2': True,
                              'modelInputView': VIEW, 'trainerArgv': ['/fake/python', '/fake/adapter', str(model)],
                              'baseArtifact': str(self.base), 'batchMin': 5, 'batchMax': 128,
                              'targetExact': .95, 'safetySeconds': 60, 'maxRssMib': 4096,
                              'goldModel': 'gpt-6-luna', 'maxGoldCalls': 20}
        pointer = {'schema_version': 1, 'artifact_path': str(self.base),
                   'artifact_sha256': shared._hash(self.base), 'cycle_id': 'base', 'model_input_view': VIEW}
        self.write_json(self.root / 'active-checkpoint.json', pointer)
        receipt = {'schema': inference_identity.SCHEMA, 'view': VIEW,
                   'verified': {'base_model_sha256': 'a' * 64},
                   'checkpoint_sha256': learning_profile.digest(pointer), 'createdAt': 1}
        self.write_json(self.root / inference_identity.RECEIPT, receipt)
        self.write_json(self.root / inference_identity.BINDING,
                        {'schema': inference_identity.SCHEMA, 'receipt_sha256': learning_profile.digest(receipt)})
        self.profile = {'autoApply': True, 'localTrain': True, 'minFreshTrainCases': 5,
                        'runtimeApprovalSha256': runtime_authorization.proposal_sha256(self.grant),
                        'view': VIEW, 'modelIdentitySha256': 'a' * 64, 'maxQualityAgeSeconds': 3600,
                        'projectRoot': str(self.project)}
        self.cfg = {'contextLearning': {'capture': True, 'autoCycle': True},
                    'sharedLearning': {'enabled': True, 'root': str(self.root),
                                       'profile': learning_profile.SCHEMA}}
        grant = lambda *a, **k: self.grant
        ready = {'ready': True, 'available_mib': 10000, 'load_per_cpu': 0.1, 'total_mib': 16384,
                 'query_status': {'memory_pressure': 'ok'},
                 'memory_pressure': {'level': 'normal', 'system_free_percent': 70}}
        real_default = general_teacher.process_pending_default
        self.real_teacher = lambda root, project, policy, **k: real_default(
            root, project, policy, environ=self.environ, **k)
        self.teacher = mock.Mock(side_effect=self.real_teacher)
        self.training = mock.Mock(side_effect=shared.run_training_pending)
        self.weekly = mock.Mock(side_effect=AssertionError('weekly quota read by a default teacher'))
        real_remaining = general_teacher.read_weekly_remaining
        self.remaining = mock.Mock(side_effect=real_remaining)
        patches = (
            (shared, '_policy', mock.Mock(side_effect=lambda root, **k: self.shared_policy)),
            (shared, '_safe_root', mock.Mock(return_value=self.root)),
            (shared, '_case_active', mock.Mock(return_value=True)),
            (shared, 'resource_report', mock.Mock(side_effect=lambda *a, **k:
                                                  dict(ready, observed_at_mono=time.monotonic()))),
            (shared, 'run_training_pending', self.training),
            (project_scope, 'require', mock.Mock(side_effect=grant)),
            (project_scope, 'present', mock.Mock(return_value=True)),
            (runtime_authorization, 'require_operation', mock.Mock(side_effect=grant)),
            (automatic_staging, 'require_operation', mock.Mock(side_effect=grant)),
            (automatic_staging, '_trainer', mock.Mock(side_effect=lambda *a, **k: FakeTrainer(self))),
            (learning_profile, 'present', mock.Mock(return_value=True)),
            (learning_profile, 'require', mock.Mock(side_effect=lambda *a, **k: self.profile)),
            (default_enrollment, 'profile_kind', mock.Mock(side_effect=lambda *a: self.kind)),
            (default_enrollment, 'default_teacher', mock.Mock(side_effect=lambda *a: self.kind == 'default-teacher')),
            (default_enrollment, 'default_local_only', mock.Mock(side_effect=lambda *a: self.kind == 'default-local')),
            (candidate_apply, 'service_transport', mock.Mock(side_effect=lambda *a: self.transport)),
            (general_teacher, 'process_pending_default', self.teacher),
            (general_teacher, 'read_weekly_quota', self.weekly),
            (general_teacher, 'read_weekly_remaining', self.remaining),
            (prequential, '_emit', mock.Mock(side_effect=lambda project, event, **f:
                                             self.recorded.append(dict(f, event=event)))),
            (prequential, 'recall_selection', mock.Mock(side_effect=lambda project, rid:
                                                        self.selections.get(rid))),
            (prequential, 'recall_selections', mock.Mock(side_effect=lambda project: dict(self.selections))))
        for target, name, value in patches:
            self.patches.enter_context(mock.patch.object(target, name, value))
        self.patches.enter_context(mock.patch('memvane.learning.context_learning.adapter.model_identity',
                                              return_value='local-model-identity'))
        self.patches.enter_context(mock.patch('memvane.settings.api.find_project_config',
                                              return_value={'config': self.cfg}))

    @staticmethod
    def write_json(path, value):
        path.write_text(canonical(value) + '\n')
        path.chmod(0o600)

    def transport(self, command, record):
        return {'status': 'prepared' if command == 'prepare' else 'activated', 'token': record['apply_id']}

    def gold(self, rid, *, empty=False, host='claude'):
        sample, bodies = edit_sample(rid, host=host)
        selected = [] if empty else [sample['candidates'][0]['candidate_id']]
        self.add(rid, sample=sample, bodies=bodies, status='gold')
        value = {'selected_ids': selected, 'input_sha256': digest(canonical(sample)), 'review': 'single_pass'}
        with shared._db(self.root) as db:
            db.execute('UPDATE cases SET gold=? WHERE request_id=?', (canonical(value), rid))
        self.gold_ids[shared._project_key(self.project)[:12] + ':' + rid] = selected
        return sample

    def golds(self, prefix, count, *, empty=0):
        for index in range(count):
            self.gold(f'{prefix}-{index:03d}', empty=index < empty)

    def worker(self):
        return project_worker.run(self.root, self.project, self.cfg)

    def jobs(self):
        with shared._db(self.root) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT manifest FROM learning_jobs ORDER BY rowid')]

    def promotions(self):
        return [row for row in self.recorded if row['event'] == 'learning.promotion']


class WorkerFlow(WorkerFixture):
    """W: the teacher's status never stops the local cycle except busy/resource/scope."""

    def run_with(self, status):
        self.teacher.side_effect = None
        self.teacher.return_value = {'status': status, 'outcomes': [], 'hosts': {}, 'quotaRemaining': None,
                                     'calls': 0}
        self.training.reset_mock()
        return self.worker()

    def test_w1_every_resting_teacher_status_runs_the_cycle_once(self):
        for status in ('drained', 'processed', 'teacher_off', 'consent_required', 'migration_pending',
                       'state_unavailable'):
            with self.subTest(status=status):
                result = self.run_with(status)
                self.assertEqual(self.training.call_count, 1)
                self.assertEqual(result['teacher']['status'], status)
                self.assertEqual(result['quota'], {'status': 'not_applicable_default_teacher'})
        self.assertEqual(self.weekly.call_count, 0)

    def test_w1_host_waits_from_real_cli_runs_the_cycle(self):
        cases = {'teacher_host_unavailable': lambda: (self.bin / 'claude').unlink(),
                 'teacher_model_unresolved': lambda: self.catalog([]),
                 'deferred_quota': lambda: self.scenario('codex', {'usedPercent': 80}),
                 'teacher_provider_waiting': lambda: self.scenario('codex', {'mode': 'quota'})}
        hosts = {'teacher_host_unavailable': 'claude', 'teacher_model_unresolved': 'codex',
                 'deferred_quota': 'codex', 'teacher_provider_waiting': 'codex'}
        for index, (reason, arrange) in enumerate(cases.items()):
            with self.subTest(reason=reason):
                host = hosts[reason]
                self.add(f'w-{index}', host=host)
                arrange()
                self.training.reset_mock()
                if reason == 'teacher_provider_waiting':
                    self.worker()
                    self.training.reset_mock()
                result = self.worker()
                self.assertEqual(result['teacher']['hosts'].get(host), reason)
                self.assertEqual(self.training.call_count, 1)
                with shared._db(self.root) as db:
                    db.execute("DELETE FROM cases")
                self.scenario('codex', {})
                self.catalog()
                from memvane.learning.tests.fake_teacher_cli_121 import install
                install(self.bin, 'claude', self.logs)
                self.expire_waits()
        self.assertEqual(self.weekly.call_count, 0)

    def test_w1_breaker_and_isolation_waits_run_the_cycle(self):
        for reason in ('teacher_host_failing', 'teacher_isolation_incomplete'):
            with self.subTest(reason=reason):
                self.teacher.side_effect = None
                self.teacher.return_value = {'status': 'processed', 'outcomes': [], 'calls': 0,
                                             'hosts': {'claude': reason}, 'quotaRemaining': None}
                self.training.reset_mock()
                self.worker()
                self.assertEqual(self.training.call_count, 1)

    def test_w1_unexpected_teacher_exception_still_runs_the_cycle(self):
        for error in (RuntimeError('fixture'), ValueError('teacher_unexpected')):
            with self.subTest(error=error):
                self.teacher.side_effect = error
                self.training.reset_mock()
                result = self.worker()
                self.assertEqual(result['teacher']['status'], 'teacher_failed')
                self.assertEqual(self.training.call_count, 1)

    def test_w2_busy_resource_and_scope_refusals_skip_the_cycle(self):
        for status in ('busy', 'deferred_resource'):
            with self.subTest(status=status):
                result = self.run_with(status)
                self.assertEqual(result['status'], status)
                self.assertEqual(self.training.call_count, 0)
        from memvane.learning.context_learning.local_cycle import ResourceDeferred
        self.teacher.side_effect = ResourceDeferred('fixture_pressure')
        self.training.reset_mock()
        self.assertEqual(self.worker()['status'], 'deferred_resource')
        for code in ('project_scope_installation_changed', 'setup_default_scope_changed'):
            with self.subTest(code=code):
                self.teacher.side_effect = ValueError(code)
                self.training.reset_mock()
                with self.assertRaisesRegex(ValueError, code):
                    self.worker()
                self.assertEqual(self.training.call_count, 0)
        self.assertFalse((self.root / 'project-last-status.json').exists())

    def test_w3_quota_read_once_per_pass_only_with_codex_cases(self):
        self.add('only-claude', host='claude')
        self.worker()
        self.assertEqual(self.remaining.call_count, 0)
        self.add('a-codex', host='codex')
        self.add('b-codex', host='codex')
        self.worker()
        self.assertEqual(self.remaining.call_count, 1)
        self.assertEqual(self.weekly.call_count, 0)
        self.assertEqual(self.training.call_count, 2)
        self.assertEqual(self.cases()['a-codex'][0], 'gold')

    def test_w4_low_codex_quota_rests_codex_only(self):
        self.scenario('codex', {'usedPercent': 75})
        self.add('cx', host='codex')
        self.add('cl', host='claude')
        result = self.worker()
        self.assertEqual(self.runs('codex'), 0)
        self.assertEqual(self.cases()['cl'][0], 'gold')
        self.assertEqual(self.cases()['cx'][0], 'queued')
        self.assertEqual(result['teacher']['hosts']['codex'], 'deferred_quota')
        self.assertEqual(self.training.call_count, 1)
        passes = self.teacher_events('pass')
        self.assertEqual(passes[-1]['quotaRemaining'], 25)
        self.assertEqual(passes[-1]['waitCodex'], 'deferred_quota')

    def test_w5_explicit_profile_keeps_the_single_case_teacher_and_quota(self):
        self.kind = 'explicit'
        with mock.patch.object(general_teacher, 'process_pending',
                               return_value={'status': 'drained'}) as explicit:
            self.weekly.side_effect = None
            self.weekly.return_value = {'remainingPercent': 80}
            result = self.worker()
        explicit.assert_called_once()
        self.assertEqual(explicit.call_args.kwargs['max_cases'], 1)
        self.assertEqual(self.teacher.call_count, 0)
        # The worker and the training retry each read it (explicit profiles only).
        self.assertEqual(self.weekly.call_count, 2)
        self.assertEqual(result['quota'], {'remainingPercent': 80})

    def test_selection_is_kept_before_a_late_gold_whatever_the_teacher_state(self):
        self.add('late-1', host='claude')
        self.selections = {'late-1': 'selector'}
        self.assertEqual(self.run_with('teacher_off')['teacher']['status'], 'teacher_off')
        with shared._db(self.root) as db:
            kept = list(db.execute('SELECT request_id,selection FROM prequential_selection'))
        self.assertEqual(kept, [('late-1', 'selector')])
        self.selections = {}  # The log window no longer holds the capture's row.
        self.teacher.side_effect = self.real_teacher
        self.worker()
        gold = self.teacher_events('gold')
        self.assertEqual([(row['request_id'], row['selection']) for row in gold], [('late-1', 'selector')])

    def test_default_local_calls_no_teacher(self):
        self.kind = 'default-local'
        result = self.worker()
        self.assertEqual(result['teacher'], {'status': 'disabled_local_only'})
        self.assertEqual(self.teacher.call_count, 0)
        self.assertEqual(self.training.call_count, 1)


class PromptCases(WorkerFixture):
    """L-3: the transition reruns the 1.0.18 prompt deletion once."""

    def test_l3_first_pass_after_transition_deletes_prompt_cases_once(self):
        self.write_json(self.root / prompt_retirement.MARKER,
                        {'schema': 'memvane-prompt-cases-retired-v1', 'count': 0, 'privateSamples': 0,
                         'retiredAtUnixMs': 1})
        sample, bodies = prompt_sample('p-old')
        self.add('p-old', sample=sample, bodies=bodies)
        self.gold('e-new')
        migrations = []
        with mock.patch('memvane.learning.context_learning.seam._private_state', return_value=None), \
                mock.patch('memvane.diagnostics.api.emit',
                        side_effect=lambda event, **f: migrations.append(dict(f, event=event))), \
                mock.patch('memvane.journal.api.select_route', return_value={'state': 'candidate'}):
            self.worker()
            self.assertEqual(sorted(self.cases()), ['e-new'])
            sample, bodies = prompt_sample('p-late')
            self.add('p-late', sample=sample, bodies=bodies)
            self.worker()
        self.assertIn('p-late', self.cases())
        rows = [row for row in migrations if row['event'] == 'learning.migration']
        self.assertEqual([row['count'] for row in rows], [1])
        marker = json.loads((self.root / prompt_retirement.MARKER).read_text())
        self.assertEqual((marker['schema'], marker['profile'], marker['count']),
                         (prompt_retirement.SCHEMA, 'default-teacher', 1))


class Learning(WorkerFixture):
    """L-1, L-7, L-8, L-9 through the worker entry."""

    def test_l1_five_gold_release_the_fresh_train_wait_with_no_holdout(self):
        self.golds('g', 4, empty=1)
        self.assertEqual(self.worker()['cycle']['status'], 'waiting_fresh_train')
        self.assertEqual(self.jobs(), [])
        self.gold('g-004')
        result = self.worker()
        self.assertEqual(result['cycle']['status'], 'staged_waiting_quality_cohort')
        self.assertEqual(result['cycle']['validation'], 0)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.trained, [[f'g-{i:03d}' for i in range(5)]])
        with shared._db(self.root) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='project_cohort_assignments'")
                             .fetchone())

    def freeze(self):
        """99 gold train a small candidate; 5 more train the frozen 104-case one."""
        self.golds('t', 99, empty=20)
        self.worker()
        self.assertEqual([len(job['members']) for job in self.jobs()], [99])
        self.golds('u', 5)
        self.worker()
        self.assertEqual([len(job['members']) for job in self.jobs()], [99, 104])
        return self.jobs()[-1]

    def evaluation(self, job):
        with shared._db(self.root) as db:
            rows = prequential.training_rows(db, self.project, self.grant)
        return [row[1] for row in prequential.split(rows, job)]

    def test_l7_promotion_sized_candidate_is_frozen_while_gold_arrives(self):
        frozen = self.freeze()
        self.assertEqual(len(self.trained), 2)
        self.golds('v', 12, empty=3)
        for _ in range(2):
            result = self.worker()
            self.assertEqual(result['cycle']['status'], 'staged_waiting_quality_cohort')
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(len(self.trained), 2)
        self.assertEqual(result['cycle']['cycle_id'], frozen['id'])
        self.assertEqual(result['cycle']['validation'], 12)
        self.assertEqual(sorted(self.evaluation(frozen)), [f'v-{i:03d}' for i in range(12)])

    def test_l8_applied_at_fifty_with_ten_empty_and_next_job_trains_them(self):
        frozen = self.freeze()
        self.golds('v', 49, empty=10)
        self.assertEqual(self.worker()['cycle']['status'], 'staged_waiting_quality_cohort')
        self.assertEqual(self.predicted, [])
        self.gold('v-049')
        result = self.worker()
        self.assertEqual(result['cycle']['status'], 'applied')
        self.assertEqual(result['cycle']['cycle_id'], frozen['id'])
        self.assertEqual(self.predicted, [50, 50])
        pointer = json.loads((self.root / 'active-checkpoint.json').read_text())
        self.assertEqual(pointer['cycle_id'], frozen['id'])
        history = prequential.read_history(self.root)
        self.assertEqual([row['cycle_id'] for row in history], [frozen['id']])
        self.gold('w-000')
        self.worker()
        jobs = self.jobs()
        self.assertEqual(len(jobs), 3)
        members = {m['request_id'] for m in jobs[-1]['members']}
        self.assertLessEqual({f'v-{i:03d}' for i in range(50)}, members)
        outcomes = [(row['outcome'], row['evalCases'], row['evalEmpty']) for row in self.promotions()]
        self.assertEqual(outcomes[:4], [('waiting', 0, 0), ('waiting', 49, 10), ('evaluated', 50, 10),
                                        ('applied', 50, 10)])
        applied = self.promotions()[3]
        self.assertEqual((applied['cycle'], applied['trainCases'], applied['candidateExact']),
                         (frozen['id'], 104, 1.0))
        self.assertEqual(applied['activeExact'], 10 / 50)

    def test_l8_rejected_at_nine_empty_boundary_then_next_job_trains_them(self):
        self.base_style, self.candidate_style = 'gold', 'empty'
        frozen = self.freeze()
        self.golds('v', 50, empty=9)
        self.assertEqual(self.worker()['cycle']['status'], 'staged_waiting_quality_cohort')
        self.assertEqual(self.predicted, [])
        self.gold('v-050', empty=True)
        result = self.worker()
        self.assertEqual((result['cycle']['status'], result['cycle']['reason']),
                         ('apply_blocked', 'apply_quality_regression'))
        self.assertEqual(json.loads((self.root / 'active-checkpoint.json').read_text())['cycle_id'], 'base')
        self.worker()
        jobs = self.jobs()
        self.assertEqual(len(jobs), 3)
        members = {m['request_id'] for m in jobs[-1]['members']}
        self.assertLessEqual({f'v-{i:03d}' for i in range(51)}, members)
        # The rejected candidate is never evaluated again.
        predicted = list(self.predicted)
        for _ in range(2):
            self.worker()
        self.assertEqual(self.predicted, predicted)
        rejected = [row for row in self.promotions() if row['outcome'] == 'rejected']
        self.assertEqual(len(rejected), 1)
        self.assertEqual((rejected[0]['cycle'], rejected[0]['reason'], rejected[0]['evalCases'],
                          rejected[0]['evalEmpty']), (frozen['id'], 'apply_quality_regression', 51, 10))
        self.assertEqual(prequential.read_history(self.root), [])

    def test_l8_all_empty_evaluation_waits_until_ten_non_empty_gold(self):
        # User decision: an always-empty candidate cannot pass on empty gold alone.
        self.base_style, self.candidate_style = 'gold', 'empty'
        frozen = self.freeze()
        self.golds('v', 50, empty=50)
        result = self.worker()
        self.assertEqual(result['cycle']['status'], 'staged_waiting_quality_cohort')
        self.assertEqual(result['cycle']['validation'], 50)
        self.assertEqual(self.predicted, [])
        self.golds('x', 9)
        self.assertEqual(self.worker()['cycle']['status'], 'staged_waiting_quality_cohort')
        self.assertEqual(self.predicted, [])
        self.gold('x-009')
        result = self.worker()
        self.assertEqual(result['cycle']['cycle_id'], frozen['id'])
        self.assertEqual(self.predicted, [60, 60])
        self.assertEqual((result['cycle']['status'], result['cycle']['reason']),
                         ('apply_blocked', 'apply_quality_regression'))
        waiting = [row for row in self.promotions() if row['outcome'] == 'waiting']
        self.assertEqual([(row['evalCases'], row['evalNonEmpty']) for row in waiting][-2:],
                         [(50, 0), (59, 9)])

    def test_l9_waiting_rows_only_on_change_and_score_rows_carry_no_text(self):
        self.freeze()
        self.golds('v', 3, empty=1)
        self.worker()
        self.worker()
        waiting = [(row['cycle'], row['evalCases']) for row in self.promotions() if row['outcome'] == 'waiting']
        self.assertEqual([count for _, count in waiting], [0, 3])
        scores = [row for row in self.recorded if row['event'] == 'learning.score']
        self.assertTrue(scores)
        for row in scores + self.promotions():
            text = json.dumps(row)
            self.assertNotIn('wiki/card', text)
            self.assertNotIn('cobalt', text)
            self.assertNotIn('Field notes', text)
            self.assertNotIn(str(self.project), text)
        self.assertEqual({row['checkpoint'] for row in scores}, {'base'})
        self.assertEqual(scores[-1]['unknownSelection'], 107)
