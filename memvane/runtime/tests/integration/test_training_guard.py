import os
import time
from unittest.mock import patch
from tests.support.case import FixtureCase
from memvane.runtime.resource_coordination import Coordinator
from memvane.runtime.types import ResourceDeferred


class TrainingGuardTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root=self.tmp / "state";self.root.mkdir(mode=0o700)
        self.clock=[0.]
        self.report={"ready":True,"observed_at_mono":0.,"total_mib":16000,"available_mib":12000,"load_per_cpu":.1,
            "query_status":{"memory_pressure":"ok"},"memory_pressure":{"level":"normal","system_free_percent":70}}

    def test_due_index_wake_is_visible_without_mutating_the_queue(self):
        from memvane.runtime import runtime_work
        queue=runtime_work.WakeQueue(self.root)
        controller=Coordinator(self.root)
        self.assertFalse(controller.priority_work())
        queue.put('learning',str(self.root/'project'),now=0)
        self.assertFalse(controller.priority_work())
        key=queue.put('laya-sync','/synthetic/index',now=time.time()+60)
        self.assertFalse(controller.priority_work())
        with queue.connect() as db:db.execute('UPDATE wakes SET due=? WHERE id=?',(time.time()-1,key))
        self.assertTrue(controller.priority_work())
        policy={'maxRssMib':512,'minAvailableMib':768,'maxLoadPerCpu':1}
        with self.assertRaisesRegex(ResourceDeferred,'learning_deferred_priority_work'):
            with Coordinator(self.root,clock=lambda:0).training(policy,self.report):
                self.fail('training admitted before due index')
        self.assertEqual(len(queue.pending()),2)
        with queue.connect() as db:db.execute('DELETE FROM wakes WHERE id=?',(key,))
        self.assertFalse(controller.priority_work())
        key=queue.put('laya-sync',str(self.root/'project'),now=time.time()-1)
        self.assertTrue(controller.priority_work())
        with queue.connect() as db:db.execute('DELETE FROM wakes WHERE id=?',(key,))
        self.assertFalse(controller.priority_work())


    def test_unsafe_wake_database_defers_training(self):
        path=self.root/'runtime-wakes.sqlite3';path.symlink_to(self.root/'missing')
        self.assertTrue(Coordinator(self.root).priority_work())


    def test_cleanup_unknown_blocks_new_common_compute_after_claim_release(self):
        c=Coordinator(self.root)
        c.cleanup_unknown(999991)
        with self.assertRaisesRegex(ResourceDeferred,'cleanup_recovery_required'):
            with c._gate('compute.lock'):self.fail('unknown group admitted')
        self.assertTrue((self.root/'cleanup-reservation.json').is_file())
        original=(self.root/'cleanup-reservation.json').read_bytes();c.cleanup_unknown(999992)
        self.assertEqual((self.root/'cleanup-reservation.json').read_bytes(),original)
        for lane in ('provider','startup'):
            with self.assertRaisesRegex(ResourceDeferred,'cleanup_recovery_required'):
                with c.activity(lane,{},self.report):self.fail('unknown group admitted')
        with self.assertRaisesRegex(ResourceDeferred,'cleanup_recovery_required'):
            with c.training({},self.report):self.fail('unknown group admitted')


    def test_real_training_guard_reads_parent_stop_pipe(self):
        from memvane.runtime import resource_coordination as coordination
        read,write=os.pipe()
        try:
            c=Coordinator(self.root)
            with c._gate('trainer.lock') as fd,patch.object(coordination,'default_root',return_value=self.root),patch.dict(os.environ,{'QMD_TRAINER_COORD_FD':str(fd),'QMD_TRAINER_STOP_FD':str(read)}):
                guard=coordination.TrainingGuard({'schema':1,'root':str(self.root),'policy':{},'namespace':'synthetic','job_id':'job'})
                guard.poll();os.write(write,b'y')
                with self.assertRaisesRegex(ValueError,'learning_yield'):guard.poll()
        finally:os.close(read);os.close(write)


    def test_training_guard_yields_before_next_group_for_due_update(self):
        from memvane.runtime import runtime_work
        from memvane.runtime import resource_coordination as coordination
        controller=Coordinator(self.root)
        with controller._gate('trainer.lock') as fd,patch.object(coordination,'default_root',return_value=self.root),patch.dict(os.environ,{'QMD_TRAINER_COORD_FD':str(fd),'QMD_TRAINER_STOP_FD':''}):
            guard=coordination.TrainingGuard({'schema':1,'root':str(self.root),'policy':{},'namespace':'synthetic','job_id':'job'})
            guard.poll()
            runtime_work.WakeQueue(self.root).put('laya-sync','/synthetic/a',now=time.time()-1)
            with self.assertRaisesRegex(ValueError,'learning_yield'):guard.poll()
