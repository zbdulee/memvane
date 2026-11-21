"""1.0.22 A-6, A-7, A-9, A-10, A-11: visibility rows, the one-row guard and the small fixes.

Events go through ``diagnostics.api.emit`` into a private fixture state root
and the logs summary is the real ``skills/logs/scripts/logs.py``. Assertions
are row counts, fields and summary lines; no prompt or card text is read back.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from memvane.diagnostics import api as diagnostics
from memvane.learning.context_learning import (default_teacher, edit_git_migration, general_teacher,
                                               prequential, project_worker, seam, shared,
                                               teacher_hosts, teacher_isolation, teacher_state)
from memvane.learning.context_learning.contracts import digest, request
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import BODY, TeacherFixture, edit_sample, sampling

LOGS = Path(__file__).resolve().parents[4] / 'skills' / 'logs' / 'scripts' / 'logs.py'
# Bound before any test patches ``diagnostics.api.emit`` to route rows here.
EMIT = diagnostics.emit


class StateRoot(TeacherFixture):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)

    def emit(self, event, **fields):
        self.assertTrue(EMIT(event, state_root=self.state, **fields))

    def rows(self, event=None):
        path = self.state / 'events.jsonl'
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        return [row for row in rows if event is None or row['event'] == event]

    def summary(self):
        result = subprocess.run([sys.executable, '-B', str(LOGS), '--state-root', str(self.state)],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class CaseLimit(StateRoot):
    def fill(self, count):
        with shared._db(self.root) as db:
            db.executemany('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,'
                           'corpus_fingerprint,captured_at,split,family_keys,use_state,status) '
                           "VALUES ('k','/p',?,'s','{}','{}','c',1,'train','[]','active','historical')",
                           [(f'fill-{i}',) for i in range(count)])

    def enqueue(self):
        sample, bodies = edit_sample('new-case')
        with mock.patch.object(shared, '_safe_root', return_value=self.root), \
                mock.patch.object(shared, '_policy', return_value={}), \
                mock.patch.object(shared, '_consent', return_value=True), \
                mock.patch.object(shared, 'derive', return_value={'body_text': 'body'}):
            return shared.enqueue({}, self.project, sample, {cid: {} for cid in bodies})

    def test_t6_1_t6_3_capture_stops_at_the_row_limit_without_deleting(self):
        self.assertEqual(shared.CASE_LIMIT, 2000)
        self.fill(1999)
        self.assertNotEqual(self.enqueue(), 'queue_full')
        with shared._db(self.root) as db:
            db.execute("DELETE FROM cases WHERE request_id='new-case'")
        self.fill_more = self.fill
        with shared._db(self.root) as db:
            db.execute("INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,"
                       "corpus_fingerprint,captured_at,split,family_keys,use_state,status) "
                       "VALUES ('k','/p','fill-last','s','{}','{}','c',1,'train','[]','active','historical')")
        self.assertEqual(self.enqueue(), 'queue_full')
        with shared._db(self.root) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cases').fetchone()[0], 2000)

    def test_t6_1_only_an_unqueued_shared_status_leaves_one_event_without_text(self):
        sample, bodies = edit_sample('seam-case')
        decision = {'state': 'selected', 'journalPath': str(self.state / 'journal.json')}
        for status, expected in (('queued', 0), ('duplicate', 0), ('queue_full', 1)):
            with self.subTest(status=status), \
                    mock.patch.object(seam, 'capture', return_value='stored'), \
                    mock.patch.object(shared, '_safe_root', return_value=self.root), \
                    mock.patch.object(edit_git_migration, 'record_fix_time'), \
                    mock.patch.object(shared, 'enqueue', return_value=status), \
                    mock.patch.object(shared, 'launch'), \
                    mock.patch('memvane.diagnostics.api.emit',
                               side_effect=lambda event, **f: self.emit(event, **{k: v for k, v in f.items()
                                                                                  if k != 'decision'})):
                before = len(self.rows('learning.capture.shared'))
                self.assertEqual(seam._store(sample, {'sharedLearning': {'enabled': True}}, self.project,
                                             self.state, {}, None, decision=decision), 'stored')
                rows = self.rows('learning.capture.shared')
                self.assertEqual(len(rows) - before, expected)
        text = json.dumps(self.rows('learning.capture.shared'))
        self.assertIn('queue_full', text)
        for secret in ('Field notes', 'cobalt', str(self.project), 'wiki/card'):
            self.assertNotIn(secret, text)

    def test_t6_2_summary_warns_near_the_limit_and_counts_queue_full(self):
        self.emit('learning.score', outcome='counts', caseRows=1850, caseLimit=2000, gold=10)
        self.emit('learning.capture.shared', outcome='queue_full')
        self.emit('learning.capture.shared', outcome='queue_full')
        out = self.summary()
        self.assertIn('WARNING learning case table: rows=1850/2000', out)
        self.assertIn('WARNING learning capture queue_full: 2', out)


class Funnel(StateRoot):
    def test_t10_1_funnel_and_promotion_progress_lines(self):
        for reason in ('skip_outside_project', 'skip_outside_project', 'skip_short_edit'):
            self.emit('posttool', outcome='empty', editRecall=reason)
        self.emit('posttool', outcome='served')
        self.emit('recall.edit', outcome='served', reason='injected')
        self.emit('learning.capture', outcome='stored')
        self.emit('learning.score', outcome='current', checkpoint='base', gold=120, candidateTrain=104,
                  candidateEval=31, candidateEvalNonEmpty=25, candidateEvalEmpty=6, caseRows=130, caseLimit=2000)
        self.emit('learning.promotion', outcome='waiting', cycle='cohort-' + '1' * 20, trainCases=104)
        out = self.summary()
        self.assertIn('edit recall funnel: posttool=4 (skip_outside_project 2, skip_short_edit 1) '
                      'recall.edit=1 stored=1', out)
        self.assertRegex(out, r'promotion progress: gold=120 candidateTrain=104/100 candidateEval=31/50 '
                              r'candidateEvalNonEmpty=25/10 candidateEvalEmpty=6/10 last=waiting ')
        self.assertNotIn('WARNING learning case table', out)

    def test_t15_funnel_counts_edit_targets(self):
        for target, reason in (('linked', None), ('linked', 'skip_throttled'), ('main', None),
                               ('outside', 'skip_outside_project'),
                               ('linked_unproved', 'skip_checkout_proof_timeout'),
                               ('mixed', 'skip_mixed_checkout'), ('elsewhere', None)):
            self.emit('posttool', outcome='empty', editRecall=reason, editTarget=target)
        self.emit('posttool', outcome='empty', editRecall='skip_outside_project')
        self.emit('recall.edit', outcome='served', reason='injected', editTarget='linked')
        self.emit('recall.edit', outcome='empty', reason='no_new_cards', editTarget='main')
        self.emit('learning.capture', outcome='stored', editTarget='linked')
        self.emit('learning.capture', outcome='edit_git_ignored', editTarget='linked')
        out = self.summary()
        self.assertIn('edit recall targets: posttool linked 2, linked_unproved 1, main 1, mixed 1, '
                      'outside 1; recall.edit linked 1, main 1; stored linked 1', out)

    def test_t10_2_no_events_no_lines(self):
        self.emit('recall', outcome='empty')
        out = self.summary()
        for line in ('edit recall funnel', 'edit recall targets', 'promotion progress', 'learning deferred'):
            self.assertNotIn(line, out)

    def test_t10_score_rows_carry_progress_counts(self):
        grant = {'projectRoot': str(self.project), 'operations': ['gold_default', 'local_train', 'quality_evaluate'],
                 'cohort': {'id': 'default-v1', 'notBeforeUnixMs': 0, 'minQualityTrainCases': 100}}
        for index in range(3):
            sample, bodies = edit_sample(f'g{index}')
            self.add(f'g{index}', sample=sample, bodies=bodies, status='gold')
            value = {'selected_ids': [], 'input_sha256': digest(canonical(sample))}
            with shared._db(self.root) as db:
                db.execute('UPDATE cases SET gold=? WHERE request_id=?', (canonical(value), f'g{index}'))
        recorded = []
        with mock.patch.object(prequential, '_emit', side_effect=lambda p, e, **f: recorded.append(dict(f, event=e))):
            prequential.record_pass(self.root, self.project, grant)
        scores = [row for row in recorded if row['event'] == 'learning.score']
        self.assertTrue(scores)
        for row in scores:
            self.assertEqual((row['gold'], row['caseRows'], row['caseLimit'], row['candidateTrain']), (3, 3, 2000, None))


class Deferral(StateRoot):
    def test_t11_1_t11_2_deferred_cycle_row_carries_reason_and_numbers(self):
        from memvane.learning.context_learning.local_cycle import record_resource_defer
        cases = {'memory_reserve': {'reason': 'memory_reserve',
                                    'memory_admission': {'reserve_mib': 1024, 'required_additional_mib': 4096},
                                    'observed': {'available_mib': 3000, 'load_per_cpu': .3, 'total_mib': 16384}},
                 'cpu_pressure': {'reason': 'cpu_pressure',
                                  'observed': {'available_mib': 9000, 'load_per_cpu': 1.4, 'total_mib': 16384}},
                 'resource_recovery_hysteresis': {'reason': 'resource_recovery_hysteresis',
                                                  'observed': {'available_mib': 9000, 'load_per_cpu': .2}}}
        policy = {'maxLoadPerCpu': .8}
        decision = {'state': 'selected'}
        for reason, admission in cases.items():
            with self.subTest(reason=reason):
                record_resource_defer(self.root, admission, reason='learning_deferred_resource')
                with mock.patch('memvane.journal.api.select_route', return_value=decision), \
                        mock.patch('memvane.diagnostics.api.emit',
                                   side_effect=lambda event, decision=None, **f: self.emit(event, **f)):
                    project_worker._record_deferral(self.root, self.project, policy,
                                                    {'status': 'deferred_resource'}, 0)
                row = self.rows('learning.cycle')[-1]
                self.assertEqual((row['outcome'], row['reason'], row['deferredBy']),
                                 ('deferred_resource', reason, 'learning_deferred_resource'))
                self.assertEqual(row['availableMib'], admission['observed']['available_mib'])
                self.assertEqual(row['loadPerCpu'], admission['observed']['load_per_cpu'])
                self.assertEqual(row['loadLimit'], .8)
                self.assertEqual(row.get('requiredMib'), 5120 if reason == 'memory_reserve' else None)
        text = json.dumps(self.rows('learning.cycle'))
        self.assertNotIn(str(self.project), text)
        self.assertNotIn(str(self.root), text)

    def routed(self):
        return (mock.patch('memvane.journal.api.select_route', return_value={'state': 'selected'}),
                mock.patch('memvane.diagnostics.api.emit',
                           side_effect=lambda event, decision=None, **f: self.emit(event, **f)))

    def worker(self, report, require=None):
        config = {'contextLearning': {'capture': True, 'autoCycle': True}}
        policy = {'maxLoadPerCpu': .8, 'minAvailableMib': 5120}
        route, emit = self.routed()
        with route, emit, mock.patch.object(shared, '_safe_root', return_value=self.root), \
                mock.patch.object(shared, '_policy', return_value=policy), \
                mock.patch.object(project_worker, 'setup_ready', return_value=True), \
                mock.patch.object(project_worker, '_retire_prompt_cases'), \
                mock.patch.object(project_worker, '_delete_unchecked_edit_cases'), \
                mock.patch.object(shared, 'resource_report', return_value=report), \
                mock.patch('memvane.learning.context_learning.project_scope.require', side_effect=require), \
                mock.patch.object(shared, 'run_training_pending',
                                  side_effect=AssertionError('the cycle must not start')):
            return project_worker.run(self.root, self.project, config)

    def test_w3_a_pass_the_worker_admission_puts_off_leaves_one_cycle_row(self):
        cases = (({'ready': False, 'available_mib': 9000, 'load_per_cpu': 1.4}, 'cpu_pressure'),
                 ({'ready': False, 'available_mib': 3000, 'load_per_cpu': .2}, 'memory_available'),
                 ({'ready': False, 'available_mib': 9000, 'load_per_cpu': .2,
                   'observation_status': 'stale'}, 'observation_stale'),
                 ({'ready': False, 'available_mib': 9000, 'load_per_cpu': .2,
                   'memory_admission': {'reason': 'memory_reserve'}}, 'memory_reserve'),
                 ({'ready': False}, 'resource_not_ready'))
        for index, (report, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                self.assertEqual(self.worker(report)['status'], 'deferred_resource')
                rows = self.rows('learning.cycle')
                self.assertEqual(len(rows), index + 1)
                row = rows[-1]
                self.assertEqual((row['outcome'], row['reason'], row['deferredBy']),
                                 ('deferred_resource', reason, 'worker_admission'))
                self.assertEqual((row.get('availableMib'), row.get('loadPerCpu')),
                                 (report.get('available_mib'), report.get('load_per_cpu')))
                self.assertEqual((row['loadLimit'], row['requiredMib']), (.8, 5120))

    def test_w3_a_scope_inventory_deferral_leaves_one_cycle_row(self):
        from memvane.learning.context_learning.local_cycle import ResourceDeferred
        answer = self.worker({'ready': True, 'available_mib': 9000, 'load_per_cpu': .2},
                             require=ResourceDeferred('learning_deferred_resource'))
        self.assertEqual(answer, {'status': 'deferred_resource', 'reason': 'learning_deferred_resource'})
        rows = self.rows('learning.cycle')
        self.assertEqual([(row['outcome'], row['reason'], row['deferredBy']) for row in rows],
                         [('deferred_resource', 'learning_deferred_resource', 'worker_admission')])
        text = json.dumps(rows)
        self.assertNotIn(str(self.project), text)
        self.assertNotIn(str(self.root), text)

    def test_w3_a_failed_event_write_does_not_change_the_pass_result(self):
        with mock.patch('memvane.journal.api.select_route', side_effect=OSError('synthetic')):
            config = {'contextLearning': {'capture': True, 'autoCycle': True}}
            with mock.patch.object(shared, '_safe_root', return_value=self.root), \
                    mock.patch.object(shared, '_policy', return_value={}), \
                    mock.patch.object(project_worker, 'setup_ready', return_value=True), \
                    mock.patch.object(project_worker, '_retire_prompt_cases'), \
                    mock.patch.object(project_worker, '_delete_unchecked_edit_cases'), \
                    mock.patch.object(shared, 'resource_report', return_value={'ready': False}):
                answer = project_worker.run(self.root, self.project, config)
        self.assertEqual(answer, {'status': 'deferred_resource', 'resources': {'ready': False}})
        self.assertEqual(self.rows('learning.cycle'), [])

    def test_w3_a_blocked_job_leaves_a_cycle_row_each_pass_that_meets_it(self):
        import inspect
        from memvane.learning.context_learning import automatic_staging
        from memvane.learning.context_learning.project_scope import SCOPE
        grant = {'scope': SCOPE, 'projectRoot': str(self.project)}
        cases = ((dict(id='cohort-' + '3' * 20, status='blocked', reason='trainer_failed', failures=3),
                  'learning_job_blocked'),
                 (dict(id='cohort-' + '4' * 20, status='blocked', reason='source_revoked', failures=0),
                  'staged_source_revoked'))
        route, emit = self.routed()
        with route, emit:
            for job, status in cases:
                self.assertEqual(automatic_staging._blocked_result(self.root, grant, job),
                                 {'status': status, 'cycle_id': job['id']})
        rows = self.rows('learning.cycle')
        self.assertEqual([(row['outcome'], row['reason'], row['failures'], row['cycle']) for row in rows],
                         [('blocked', job['reason'], job['failures'], job['id']) for job, _ in cases])
        # The staged preparation answers a blocked job through this one function.
        source = inspect.getsource(automatic_staging._prepare_staged)
        self.assertEqual(source.count("if job['status']=='blocked':return _blocked_result(root,grant,job)"), 1)
        self.assertNotIn("'learning_job_blocked'", source)

    def test_w3_summary_and_slo_count_blocked_and_put_off_cycles(self):
        self.emit('learning.cycle', outcome='deferred_resource', reason='cpu_pressure', deferredBy='worker_admission')
        self.emit('learning.cycle', outcome='blocked', reason='trainer_failed', failures=3)
        self.assertRegex(self.summary(), r'learning deferred: cpu_pressure 1; last trained none')
        result = subprocess.run([sys.executable, '-B', str(LOGS), '--state-root', str(self.state), '--slo'],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'slo learning: ok stored=0 gold=0 trained=0 failed=0 '
                                        r'deferred_resource=1 blocked=1 rearmed=0 withoutReason=0')

    def test_t11_3_summary_counts_deferrals_and_the_last_training(self):
        for reason in ('memory_reserve', 'memory_reserve', 'cpu_pressure'):
            self.emit('learning.cycle', outcome='deferred_resource', reason=reason)
        self.emit('learning.cycle', outcome='trained', cycle='cohort-' + '2' * 20, trainCases=100)
        out = self.summary()
        self.assertRegex(out, r'learning deferred: cpu_pressure 1, memory_reserve 2; last trained \d{4}-')


    def test_summary_shows_selector_maintenance_rotation_and_daily_gold(self):
        # 1.0.23: the daily look at learning needs these three in the summary.
        self.emit('learning.selector.maintenance', outcome='started')
        self.emit('learning.selector.maintenance', outcome='completed')
        for retries in (1, 2, 7):
            self.emit('learning.selector.maintenance', outcome='deferred',
                      reason='learning_prepare_busy', busyRetries=retries)
        self.emit('learning.selector.maintenance', outcome='deferred', reason='resource_deferred')
        self.emit('learning.selector.maintenance', outcome='failed', reason='renewal_refused')
        for count in (100, 3):
            self.emit('learning.rotation', outcome='deleted', count=count, caseRows=1800, limit=1800)
        self.emit('learning.teacher', outcome='gold', reason='single_pass', request_id='r1', usageIn=10)
        self.emit('learning.teacher', outcome='gold', reason='parked_answer', request_id='r2')
        self.emit('learning.teacher', outcome='pending_review', reason='gold_attempt_failed',
                  request_id='r3')
        out = self.summary()
        self.assertIn('selector maintenance: completed=1 deferred=4 (learning_prepare_busy 3, '
                      'resource_deferred 1) failed=1 (renewal_refused 1) busyRetriesMax=7', out)
        self.assertIn('learning rotation: deleted=103 passes=2', out)
        # Two calls (one gold, one set aside) and the gold a kept answer became without a call.
        self.assertRegex(out, r'teacher \d{4}-\d\d-\d\d: calls=2 gold=2 failed=1 '
                              r'\(gold_attempt_failed 1\) usageIn=10')

    def test_summary_names_the_unfinished_maintenance_step_and_idle_rotation(self):
        for step in ('warm_status', 'warm_status', 'publish_owner_busy'):
            self.emit('learning.selector.maintenance', outcome='deferred',
                      reason='maintenance_incomplete', incomplete=step, stage='warm')
        self.emit('learning.rotation', outcome='nothing_closed', count=0, caseRows=1900,
                  protected=1900, limit=1800)
        self.emit('learning.rotation', outcome='deferred', reason='case_rotation_changed')
        out = self.summary()
        self.assertIn('selector maintenance: completed=0 deferred=3 (maintenance_incomplete 3) failed=0 '
                      'incomplete(publish_owner_busy 1, warm_status 2)', out)
        self.assertIn('learning rotation: deleted=0 passes=0 (deferred 1, nothing_closed 1)', out)

    def test_summary_has_no_maintenance_or_rotation_line_without_rows(self):
        self.emit('learning.selector.maintenance', outcome='started')
        out = self.summary()
        self.assertNotIn('selector maintenance:', out)
        self.assertNotIn('learning rotation:', out)


class OneRowGuard(TeacherFixture):
    def test_t7_1_running_guard_reads_one_case_row(self):
        for index in range(500):
            self.add(f'r{index:03d}', tag=str(index))
        self.scenario('claude', {'mode': 'sleep', 'seconds': 2})
        full = mock.Mock(side_effect=prequential.admitted)
        one = mock.Mock(side_effect=prequential.admitted_one)
        with mock.patch.object(prequential, 'admitted', full), mock.patch.object(prequential, 'admitted_one', one):
            self.run_pass(max_cases=1)
        self.assertEqual(self.cases()['r000'][0], 'gold')
        # The pass lists its targets and per-host counts; the running guard never does.
        self.assertEqual(full.call_count, 3)
        self.assertGreaterEqual(one.call_count, 4)
        self.assertEqual({c.args[3] for c in one.call_args_list}, {'r000'})

    def test_t7_2_one_row_and_full_admission_agree(self):
        grant = {'projectRoot': str(self.project), 'cohort': {'notBeforeUnixMs': 2_000_000}}
        self.add('ok', captured_at=3000)
        self.add('early', captured_at=1000)
        self.add('gold-no-label', status='gold', captured_at=3001)
        self.add('done', status='pending_review', captured_at=3002)
        sample, bodies = edit_sample('tampered')
        self.add('tampered', sample=sample, bodies=bodies, captured_at=3003)
        with shared._db(self.root) as db:
            db.execute("UPDATE cases SET input_sha256='0' WHERE request_id='tampered'")
            for statuses in (('queued',), ('queued', 'gold', 'pending_review')):
                full = {row[1] for _, row in prequential.admitted(db, self.project, grant, statuses=statuses)}
                for rid in ('ok', 'early', 'gold-no-label', 'done', 'tampered', 'missing'):
                    one = prequential.admitted_one(db, self.project, grant, rid, statuses=statuses)
                    self.assertEqual(one is not None, rid in full, (rid, statuses))


class SmallFixes(StateRoot):
    def test_a9a_stopped_requests_without_an_intent_say_deferred(self):
        state = teacher_state.read_state(self.root)
        state['enable'].update(failures=teacher_state.ENABLE_FAILURE_LIMIT, pluginVersion='9.9.9')
        self.assertTrue(teacher_state.write_state(self.root, state))
        lines = teacher_state.session_notices(self.root, 'claude', '9.9.9')
        self.assertEqual(lines, [teacher_state.ENABLE_DEFERRED_NOTICE])
        self.assertNotIn('멈췄습니다', lines[0])
        (self.root / teacher_state.ENABLE_INTENT).write_text('{}')
        self.assertEqual(teacher_state.session_notices(self.root, 'claude', '9.9.9'), [teacher_state.ENABLE_NOTICE])

    def test_a9b_a_cli_in_an_unsafe_folder_is_told_apart_and_still_refused(self):
        unsafe = self.home / 'shared-bin'
        unsafe.mkdir()
        unsafe.chmod(0o775)
        os.symlink(self.bin / 'claude', unsafe / 'claude')
        environ = dict(self.environ, PATH=f'{unsafe}:/usr/bin:/bin')
        ready = teacher_hosts.prepare('claude', project=self.project, environ=environ,
                                      system_prompt=default_teacher.INSTRUCTION_V2)
        self.assertEqual(ready, {'ready': False, 'reason': teacher_state.UNSAFE_PATH})
        empty = dict(self.environ, PATH='/usr/bin:/bin')
        self.assertEqual(teacher_hosts.prepare('claude', project=self.project, environ=empty,
                                               system_prompt=default_teacher.INSTRUCTION_V2)['reason'],
                         teacher_state.UNAVAILABLE)
        row = teacher_state.read_state(self.root)['hosts']['claude']
        row.update(reason=teacher_state.UNSAFE_PATH, queued=1)
        line = teacher_state._host_notice(row, 'claude', teacher_state.now_ms())[1]
        self.assertIn('폴더의 권한', line)

    def test_a9c_redirected_codex_home_is_a_long_rest_reason(self):
        self.assertEqual(teacher_state.REST_REASONS[teacher_hosts.CODEX_HOME_REDIRECTED], 'CODEX_HOME 재지정')

    def test_a9d_a_checked_teacher_env_holds_none_of_the_provider_keys(self):
        # The runtime side (``memvane/runtime/tests/unit/test_provider_env_122.py``) pins that the
        # provider adds only ``PROVIDER_ENV_KEYS``; the isolation check runs before they are added.
        keys = {'OMP_NUM_THREADS', 'QMD_CPU_THREADS', 'TOKENIZERS_PARALLELISM', 'QMD_PROVIDER_COORD_FD'}
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                env = teacher_hosts.child_environment(host, self.environ)
                teacher_isolation.check_env(host, env, parent=self.environ)
                self.assertFalse(keys & set(env))

    def test_a9e_the_hidden_security_policy_flag_is_gone(self):
        from memvane.learning import context_learning_cli
        for argv in (['label-request', 'r1', '--host', 'claude', '--accept-security-policy'],
                     ['budgeted-label-request', 'r1', 'a1', '--policy-json', '{}', '--accept-security-policy']):
            with self.subTest(argv=argv[0]), mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit):
                    context_learning_cli.parser().parse_args(argv)

    def test_t9_1_a_hermes_sample_never_becomes_a_case(self):
        rows = [{'candidate_id': 'wiki/card0.md', 'revision_sha256': digest(BODY), 'excerpt': BODY, 'eligible': True}]
        try:
            sample = request({'prompt': 'Field notes for the garden.', 'context_origin': 'tool_edit'}, rows,
                             host='hermes', request_id='hermes-1', sampling=sampling(['wiki/card0.md'], []))
        except (ValueError, KeyError, TypeError):
            sample = None
        if sample is not None:
            with mock.patch.object(shared, '_safe_root', return_value=self.root), \
                    mock.patch.object(shared, '_policy', return_value={}), \
                    mock.patch.object(shared, '_consent', return_value=True):
                try:
                    status = shared.enqueue({}, self.project, sample, {'wiki/card0.md': {}})
                except (ValueError, KeyError, TypeError):
                    status = 'refused'
            self.assertIn(status, ('ineligible', 'refused'))
        with shared._db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cases WHERE request_id='hermes-1'").fetchone()[0], 0)
