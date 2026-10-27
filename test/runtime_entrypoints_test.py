"""Remaining entry-point admission: fake observations and private files only."""
from contextlib import ExitStack
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from context_learning.resource_coordination import Coordinator
from context_learning.local_cycle import ResourceDeferred


class EntryPoints(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('os.cpu_count',return_value=18))
        self.stack.enter_context(patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_PROVIDER_COORD_FD':''}))
        name=self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root=Path(name).resolve();self.root.chmod(0o700)
        self.report={'ready':True,'observed_at_mono':time.monotonic(),
            'total_mib':32000,'available_mib':25000,'load_per_cpu':.1,
            'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'level':'normal','system_free_percent':75}}
        self.c=Coordinator(self.root)

    def admission(self):
        self.assertIsNotNone(importlib.util.find_spec('runtime_admission'),'remaining entry-point facade missing')
        import runtime_admission as a
        self.stack.enter_context(patch.object(a,'Coordinator',return_value=self.c))
        self.stack.enter_context(patch.object(a,'sample',return_value=self.report))
        return a

    def test_remote_lane_is_global_but_does_not_hold_inference_compute(self):
        a=self.admission()
        with a.provider() as fd:
            self.assertGreaterEqual(fd,0)
            with self.c.inference_compute(time.monotonic()+.05):pass
            with self.assertRaises(ResourceDeferred):
                with Coordinator(self.root).activity('provider',a.POLICY,self.report):self.fail('second provider')
        with self.c.activity('provider',a.POLICY,self.report):pass

    def test_metadata_quiescence_excludes_provider_without_model_admission(self):
        a=self.admission()
        with self.c.quiesce_provider():
            with self.c.inference_compute(time.monotonic()+.05):pass
            with self.assertRaises(ResourceDeferred):
                with Coordinator(self.root).activity('provider',a.POLICY,self.report):
                    self.fail('selected writer crossed metadata fence')
        with self.c.activity('provider',a.POLICY,self.report):pass

    def test_foreground_demand_refuses_new_remote_work_without_waiting(self):
        a=self.admission()
        with self.c.inference(time.monotonic()+1,'search'):
            with self.assertRaises(ResourceDeferred):
                with a.provider():self.fail('provider launched')

    def test_unknown_or_stale_observations_cannot_start_a_provider(self):
        a=self.admission()
        for report in (None,{**self.report,'observed_at_mono':time.monotonic()-3}):
            with patch.object(a,'sample',return_value=report):
                with self.assertRaises(ResourceDeferred):
                    with a.provider():self.fail('unknown admitted')

    def test_general_teacher_deferral_precedes_scope_and_reservation(self):
        a=self.admission()
        from context_learning import general_teacher as teacher
        with patch.object(a,'sample',return_value=None),patch.object(teacher,'_policy') as policy:
            result=teacher.process_pending(self.root,self.root,{})
        self.assertEqual(result['status'],'deferred_resource');policy.assert_not_called()

    def test_synthetic_teacher_deferral_precedes_source_and_budget(self):
        a=self.admission()
        from context_learning import synthetic_teacher as teacher
        with patch.object(a,'sample',return_value=None),patch.object(teacher,'policy_for') as policy:
            result=teacher.process_pending(self.root,self.root,{})
        self.assertEqual(result['status'],'deferred_resource');policy.assert_not_called()

    def test_custom_wiki_extractor_defer_never_launches_child(self):
        a=self.admission();import wiki_compile_worker as worker
        with patch.object(a,'sample',return_value=None),patch.object(worker.subprocess,'Popen') as spawn:
            result=worker.run_extractor(['/synthetic/provider'],{},1,self.root)
        self.assertEqual(result,(None,'resource_deferred',75));spawn.assert_not_called()

    def test_preparation_hash_matches_bytes_and_yields_to_search(self):
        a=self.admission();path=self.root/'bytes';path.write_bytes(b'abcd'*300000)
        with a.preparation():
            self.assertEqual(a.hash_file(path),hashlib.sha256(path.read_bytes()).hexdigest())
            with self.c.inference(time.monotonic()+1,'search'):
                with self.assertRaises(ResourceDeferred):a.hash_file(path)
        with self.c.inference_compute(time.monotonic()+.05):pass

    def test_preparation_budget_refuses_next_chunk_and_cleans_up(self):
        a=self.admission();clock=[0.]
        with self.assertRaises(ResourceDeferred):
            with a.preparation(clock=lambda:clock[0],max_seconds=1):
                clock[0]=2;a.preparation_checkpoint()
        with self.c.inference_compute(time.monotonic()+.05):pass

    def test_startup_does_not_deadlock_its_own_foreground_demand(self):
        a=self.admission()
        with self.c.inference(time.monotonic()+1,'cold search'):
            with a.startup():
                with self.assertRaises(ResourceDeferred):
                    with self.c.inference_compute(time.monotonic()):self.fail('startup overlap')
        with self.c.inference_compute(time.monotonic()+.05):pass

    def test_existing_healthy_endpoint_does_not_require_cold_admission(self):
        a=self.admission();import qmd_endpoint as endpoint
        row={'pid':12345,'port':8584,'url':'http://127.0.0.1:8584','runtime':'a','store':'b'}
        with patch.object(endpoint,'limits',return_value=(8584,1,.1)),patch.object(endpoint,'expected',return_value={'runtime':'a','store':'b'}),patch.object(endpoint,'raw',return_value=row),patch.object(endpoint,'alive',return_value=True),patch.object(endpoint,'owned',return_value=True),patch.object(endpoint,'healthy',return_value=True),patch.object(a,'sample',side_effect=AssertionError('cold probe on reuse')):
            self.assertEqual(endpoint.ensure_locked(self.root),row)

    def test_cold_refusal_creates_no_daemon(self):
        a=self.admission();import qmd_endpoint as endpoint
        with patch.object(endpoint,'limits',return_value=(8584,1,.1)),patch.object(endpoint,'expected',return_value={}),patch.object(endpoint,'raw',return_value=None),patch.object(endpoint,'log_path',return_value=self.root/'daemon.log'),patch.object(endpoint,'spawn') as spawn,patch.object(a,'sample',return_value=None):
            with self.assertRaises(ResourceDeferred):endpoint.ensure_locked(self.root)
        spawn.assert_not_called()

    def test_warm_without_server_cancellation_is_deferred_even_when_healthy(self):
        a=self.admission()
        with patch.object(a.subprocess,'Popen',side_effect=AssertionError('warm request sent')):
            result=a.optional_warm()
        self.assertEqual(result['status'],'deferred_resource')
        self.assertEqual(result['reason'],'warm_server_deadline_unavailable')

    def test_scope_inventory_defers_before_scanning_installations(self):
        a=self.admission();from context_learning import project_scope as scope
        with patch.object(a,'sample',return_value=None),patch.object(scope,'_envelope') as read:
            with self.assertRaises(ResourceDeferred):scope.require(self.root,{},'local_train',fresh_inventory=True)
        read.assert_not_called()

    def test_automatic_training_defers_before_planning_or_hashing(self):
        a=self.admission();from context_learning import automatic_staging as stage
        with patch.object(a,'sample',return_value=None),patch.object(stage,'training_and_validation') as rows:
            result=stage.train_due_staged(self.root,{},{} )
        self.assertEqual(result['status'],'deferred_resource');rows.assert_not_called()
        import json
        self.assertFalse(json.loads((self.root/'resource-defer.json').read_text())['completionAck'])

    def test_cohort_preparation_defers_before_reading_all_history(self):
        a=self.admission();from context_learning import general_cohort as cohort
        db=Mock();db.execute.side_effect=AssertionError('history read before admission')
        with patch.object(a,'sample',return_value=None):
            with self.assertRaises(ResourceDeferred):cohort.admitted(db,self.root,{'projectRoot':str(self.root),'cohort':{'id':'fixture','notBeforeUnixMs':1}})
        db.execute.assert_not_called()

    def test_budgeted_manual_teacher_defers_without_a_reservation(self):
        a=self.admission();from context_learning import teacher_budget as budget
        with patch.object(a,'sample',return_value=None),patch.object(budget,'reserve_review') as reserve:
            result=budget.budgeted_review_request(self.root,'case','attempt',{})
        self.assertEqual(result['status'],'deferred_resource');reserve.assert_not_called()

    def test_provider_child_inherits_only_its_lane_and_exact_environment(self):
        a=self.admission()
        class Child:
            pid=22222;returncode=0;stdin=stdout=stderr=None
            def communicate(self,*args,**kwargs):return 'answer',''
        def spawn(argv,**options):
            self.assertEqual(options['env']['INDEX_PATH'],'/synthetic/index')
            fd=int(options['env']['QMD_PROVIDER_COORD_FD'])
            self.assertIn(fd,options['pass_fds'])
            self.assertEqual(os.fstat(fd).st_ino,(self.root/'provider.lock').stat().st_ino)
            with self.c.inference_compute(time.monotonic()+.05):pass
            return Child()
        from context_learning import transport
        with patch.object(a.subprocess,'Popen',side_effect=spawn),patch.object(transport,'_stop_owned',return_value=True):
            result=a.provider_run(['/synthetic/provider'],env={'INDEX_PATH':'/synthetic/index'},timeout=1,capture_output=True,text=True)
        self.assertEqual(result.stdout,'answer')

    def test_endpoint_start_lock_contention_is_immediate_defer(self):
        a=self.admission();import qmd_endpoint as endpoint
        import fcntl
        path=self.root/'endpoint-start.lock';path.touch(mode=0o600)
        fd=os.open(path,os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX)
        def nonblocking(lock,flags):
            self.assertTrue(flags&fcntl.LOCK_NB,'endpoint lock could wait indefinitely')
            return fcntl.flock(lock,flags)
        try:
            with patch.object(endpoint,'root',return_value=self.root),patch.object(endpoint.install_deadline,'flock',side_effect=nonblocking):
                with self.assertRaises(ResourceDeferred):
                    with endpoint.locked():self.fail('second starter')
        finally:os.close(fd)

    def test_an_unlocked_inherited_fd_cannot_bypass_resource_admission(self):
        a=self.admission()
        path=self.root/'provider.lock';path.touch(mode=0o600)
        fd=os.open(path,os.O_RDWR)
        try:
            with patch.dict(os.environ,{'QMD_PROVIDER_COORD_FD':str(fd)}),patch.object(a,'sample',return_value=None):
                with self.assertRaises(ResourceDeferred):
                    with a.provider():self.fail('unlocked fd bypassed admission')
        finally:os.close(fd)

    def test_inherited_held_fd_reuses_lane_and_closes_only_its_duplicate(self):
        a=self.admission()
        with a.provider() as fd:
            from contextvars import Context
            def nested():
                with patch.dict(os.environ,{'QMD_PROVIDER_COORD_FD':str(fd)}):
                    with a.provider() as duplicate:self.assertNotEqual(fd,duplicate)
            Context().run(nested)
            os.fstat(fd)
            with self.assertRaises(ResourceDeferred):
                with Coordinator(self.root).activity('provider',a.POLICY,self.report):self.fail('lease lost')

    def test_cold_timeout_releases_compute_and_never_inherits_it_to_daemon(self):
        a=self.admission();import qmd_endpoint as endpoint
        child=Mock(pid=22222);child.poll.return_value=None
        child.wait.return_value=0
        with patch.object(endpoint,'limits',return_value=(8584,1,.1)),patch.object(endpoint,'expected',return_value={}),patch.object(endpoint,'raw',return_value=None),patch.object(endpoint,'log_path',return_value=self.root/'daemon.log'),patch.object(endpoint,'port_busy',return_value=False),patch.object(endpoint,'spawn',return_value=child),patch.object(endpoint,'bound',return_value=set()),patch.object(endpoint.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'timeout'):endpoint.ensure_locked(self.root)
        child.terminate.assert_called_once()
        with self.c.inference_compute(time.monotonic()+.05):pass
        def spawn_cpu_only(*args,**options):
            self.assertEqual(len(options['pass_fds']),1)
            actual=os.fstat(options['pass_fds'][0])
            expected=(self.root/'cpu-share-0.lock').stat()
            self.assertEqual((actual.st_dev,actual.st_ino),(expected.st_dev,expected.st_ino))
            return child
        with patch('context_learning.resource_coordination.Coordinator',return_value=self.c),patch.object(endpoint,'child_environment',return_value={}),patch.object(endpoint.subprocess,'Popen',side_effect=spawn_cpu_only) as spawn:
            endpoint.spawn(8584,subprocess.DEVNULL)
        self.assertTrue(spawn.call_args.kwargs['pass_fds'])  # CPU only; never compute.

    def test_final_launch_rechecks_even_an_existing_provider_lease(self):
        a=self.admission()
        with a.provider(),patch.object(a,'sample',return_value=None),patch.object(a.subprocess,'Popen') as spawn:
            with self.assertRaises(ResourceDeferred):a.provider_run(['/synthetic/provider'],timeout=1)
        spawn.assert_not_called()

    def test_already_owned_compute_can_validate_scope_without_recursive_lock(self):
        a=self.admission()
        with self.c.inference(time.monotonic()+1,'apply validation'),self.c.inference_compute(time.monotonic()+1):
            with a.preparation():
                a.preparation_checkpoint()
                with self.assertRaises(ResourceDeferred):
                    with Coordinator(self.root).step(a.POLICY,self.report):self.fail('borrow lost compute')
        with self.c.inference_compute(time.monotonic()+.05):pass

    def test_recall_can_prepare_with_its_owned_demand_before_compute(self):
        a=self.admission()
        import runtime_work
        with runtime_work.foreground('recall',coordinator=self.c):
            with a.preparation():
                a.preparation_checkpoint()
                self.assertIsNotNone(self.c.owned_compute())
        with self.c.inference_compute(time.monotonic()+.05):pass

    def test_foreground_preparation_has_one_second_budget(self):
        a=self.admission();clock=[0.]
        with self.c.inference(time.monotonic()+1,'query'):
            with self.assertRaisesRegex(ResourceDeferred,'preparation_budget'):
                with a.preparation(clock=lambda:clock[0]):
                    clock[0]=1.1;a.preparation_checkpoint()

    def test_a_slow_observation_cannot_admit_after_the_preparation_deadline(self):
        a=self.admission();clock=[0.]
        def slow(*args,**kwargs):clock[0]=2.;return self.report
        with patch.object(a,'sample',side_effect=slow):
            with self.assertRaisesRegex(ResourceDeferred,'preparation_budget'):
                with a.preparation(clock=lambda:clock[0],max_seconds=1):self.fail('late probe admitted')

    def test_mac_probes_share_the_remaining_observation_deadline(self):
        self.admission();from context_learning import shared
        clock=[100.];timeouts=[]
        def probe(argv,**options):
            timeouts.append(options['timeout']);clock[0]+=options['timeout']
            raise subprocess.TimeoutExpired(argv,options['timeout'])
        with patch.object(shared.sys,'platform','darwin'),patch.object(shared.time,'monotonic',side_effect=lambda:clock[0]),patch.object(Path,'is_file',return_value=False),patch.object(shared.subprocess,'run',side_effect=probe):
            report=shared.resource_report({'maxRssMib':512,'minAvailableMib':768,'maxLoadPerCpu':1},detailed=True,deadline=100.3)
        self.assertFalse(report['ready']);self.assertEqual(len(timeouts),1)
        self.assertAlmostEqual(timeouts[0],.3)

    def test_another_context_cannot_borrow_a_foreground_slot(self):
        a=self.admission();from contextvars import Context
        def background():
            with self.assertRaises(ResourceDeferred):
                with a.preparation():self.fail('borrowed unrelated foreground')
        with self.c.inference(time.monotonic()+1,'query'):Context().run(background)

    def test_orphan_startup_reservation_outlives_kernel_parent_lease(self):
        a=self.admission()
        with a.startup() as startup:
            self.assertTrue(hasattr(startup,'pending'),'startup lacks durable ownership')
            startup.pending()  # simulate a parent loss before a safe child outcome
        self.assertTrue((self.root/'startup-reservation.json').is_file())
        for gate in (self.c.step(a.POLICY,self.report),self.c.inference_compute(time.monotonic()+.05),a.startup()):
            with self.assertRaises(ResourceDeferred):
                with gate:self.fail('orphan reservation ignored')

    def test_remote_launch_cannot_wait_while_owning_compute(self):
        a=self.admission()
        with self.c._gate('compute.lock'),patch.object(a.subprocess,'Popen',side_effect=AssertionError('remote under compute')):
            with self.assertRaises(ResourceDeferred):a.provider_run(['/synthetic/provider'],timeout=1)

    def test_apply_direct_compute_gate_can_borrow_preparation(self):
        a=self.admission()
        with self.c._gate('compute.lock'):
            with a.preparation():a.preparation_checkpoint()
        with self.c.inference_compute(time.monotonic()+.05):pass

    def test_final_resource_rejection_is_a_proved_pre_send_receipt(self):
        a=self.admission()
        spec=importlib.util.spec_from_file_location('gold_fixture',Path(__file__).parent/'fixtures/context-learning-general-teacher-checks.py')
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        case=fixture.Checks();case.setUp();self.addCleanup(case.tearDown);case.case()
        def invoke(*args,**options):
            with patch.object(a,'sample',return_value=None):
                return a.provider_popen(['/synthetic/provider'],before_launch=options['before_launch'],defer_launch=options['defer_launch'])
        with patch.object(a.subprocess,'Popen',side_effect=AssertionError('must not send')):
            result=case.run_queue(invoke)
        self.assertEqual(result['status'],'deferred_resource')
        with case.db() as db:
            self.assertEqual(db.execute('SELECT stage,error_code FROM project_gold_attempts').fetchone(),('pre_send_rejected','resource_deferred'))
            self.assertEqual(db.execute('SELECT status FROM cases').fetchone()[0],'pending_review')


if __name__=='__main__':unittest.main()
