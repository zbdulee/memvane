from unittest.mock import patch
from tests.support.case import FixtureCase
from memvane.runtime.resource_coordination import Coordinator
from memvane.runtime.types import ResourceDeferred
from memvane.runtime import runtime_work as runtime


class Hardening(FixtureCase):
    def setUp(self):
        super().setUp()
        topology=patch("os.cpu_count",return_value=18);topology.start();self.addCleanup(topology.stop)
        self.root=self.tmp / "state";self.root.mkdir(mode=0o700)
        self.policy={"maxRssMib":512,"minAvailableMib":512,"maxLoadPerCpu":1}
        self.report={"ready":True,"total_mib":16000,"available_mib":12000,"load_per_cpu":.1,
            "query_status":{"memory_pressure":"ok"},"memory_pressure":{"level":"normal","system_free_percent":70},
            "observed_at_mono":10.}

    def test_pressure_recovery_needs_five_stable_seconds_across_instances(self):
        clock=[10.]
        def controller():return Coordinator(self.root,clock=lambda:clock[0],wall=lambda:100+clock[0])
        def report(**kw):return {**self.report,'observed_at_mono':clock[0],**kw}
        with self.assertRaises(ResourceDeferred):
            with controller().step(self.policy,report(ready=False)):self.fail('pressure admitted')
        clock[0]=11
        with self.assertRaises(ResourceDeferred):
            with controller().step(self.policy,report()):self.fail('instant rebound admitted')
        clock[0]=16
        with controller().step(self.policy,report()):pass


    def test_borderline_memory_does_not_clear_recovery_latch(self):
        clock=[10.];c=Coordinator(self.root,clock=lambda:clock[0],wall=lambda:100+clock[0])
        def attempt(available):
            with c.step(self.policy,{**self.report,'available_mib':available,'observed_at_mono':clock[0]}):pass
        # The actual absolute requirement is task512 + system512, not a RAM ratio.
        with self.assertRaises(ResourceDeferred):attempt(1000)
        clock[0]=20
        with self.assertRaises(ResourceDeferred):attempt(1023)
        clock[0]=30
        with self.assertRaises(ResourceDeferred):attempt(1023)


    def test_unobserved_waiter_gap_restarts_the_recovery_window(self):
        clock=[10.];c=Coordinator(self.root,clock=lambda:clock[0],wall=lambda:100+clock[0])
        def attempt(**kw):
            with c.step(self.policy,{**self.report,'observed_at_mono':clock[0],**kw}):pass
        with self.assertRaises(ResourceDeferred):attempt(ready=False)
        clock[0]=11
        with self.assertRaises(ResourceDeferred):attempt()
        clock[0]=1000
        with self.assertRaises(ResourceDeferred):attempt()
        clock[0]=1005
        attempt()


    def test_oversized_wake_target_is_rejected_without_a_new_row(self):
        q=runtime.WakeQueue(self.root)
        with self.assertRaises(ValueError):q.put('learning','/'+('x'*4096))
        self.assertEqual(q.pending(),[])
