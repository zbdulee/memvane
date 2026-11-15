from contextlib import ExitStack
import hashlib
import importlib.util
import os
import time
from unittest.mock import Mock, patch
from tests.support.case import FixtureCase
from memvane.runtime.resource_coordination import Coordinator
from memvane.runtime.types import ResourceDeferred


class EntryPoints(FixtureCase):
    def setUp(self):
        super().setUp()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("os.cpu_count",return_value=18))
        self.stack.enter_context(patch.dict(os.environ,{"QMD_SETUP_GUARD_FIXTURE":"","QMD_PROVIDER_COORD_FD":""}))
        self.root=self.tmp / "state";self.root.mkdir(mode=0o700)
        self.report={"ready":True,"observed_at_mono":time.monotonic(),
            "total_mib":32000,"available_mib":25000,"load_per_cpu":.1,
            "query_status":{"memory_pressure":"ok"},"memory_pressure":{"level":"normal","system_free_percent":75}}
        self.c=Coordinator(self.root)

    def admission(self):
        self.assertIsNotNone(importlib.util.find_spec('memvane.runtime.runtime_admission'),'remaining entry-point facade missing')
        from memvane.runtime import runtime_admission as a
        self.stack.enter_context(patch.object(a,'Coordinator',return_value=self.c))
        self.stack.enter_context(patch.object(a,'sample',return_value=self.report))
        return a

    def test_custom_wiki_extractor_defer_never_launches_child(self):
        from memvane.wiki.engines import api as engines
        admission = self.admission()
        with patch.object(admission, 'sample', return_value=None), \
                patch.object(admission.subprocess, 'Popen') as spawn:
            result = engines.run_extractor(['/synthetic/provider'], {}, 1, self.root)
        self.assertEqual(result, (None, 'resource_deferred', 75))
        spawn.assert_not_called()


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


    def test_warm_without_server_cancellation_is_deferred_even_when_healthy(self):
        a=self.admission()
        with patch.object(a.subprocess,'Popen',side_effect=AssertionError('warm request sent')):
            result=a.optional_warm()
        self.assertEqual(result['status'],'deferred_resource')
        self.assertEqual(result['reason'],'warm_server_deadline_unavailable')


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
        from memvane.kernel import process
        with patch.object(a.subprocess,'Popen',side_effect=spawn),patch.object(process,'stop_owned',return_value=True):
            result=a.provider_run(['/synthetic/provider'],env={'INDEX_PATH':'/synthetic/index'},timeout=1,capture_output=True,text=True)
        self.assertEqual(result.stdout,'answer')


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
        from memvane.runtime import runtime_work
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
