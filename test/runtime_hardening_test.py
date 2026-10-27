"""Fake signals and private files only; no live probes, models or host CLI."""
from pathlib import Path
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from context_learning.resource_coordination import Coordinator,decision
from context_learning.local_cycle import ResourceDeferred
import runtime_work as runtime


class Hardening(unittest.TestCase):
    def setUp(self):
        topology=patch('os.cpu_count',return_value=18);topology.start();self.addCleanup(topology.stop)
        env=patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_SANDBOX':''})
        env.start();self.addCleanup(env.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.policy={'maxRssMib':512,'minAvailableMib':512,'maxLoadPerCpu':1}
        self.report={'ready':True,'total_mib':16000,'available_mib':12000,'load_per_cpu':.1,
            'query_status':{'memory_pressure':'ok'},'memory_pressure':{'level':'normal','system_free_percent':70},
            'observed_at_mono':time.monotonic()}

    def test_missing_old_future_and_nonfinite_observations_defer(self):
        for value in (None,time.monotonic()-10,time.monotonic()+10,float('nan')):
            report={**self.report,'observed_at_mono':value}
            self.assertFalse(decision(self.policy,report)['ready'])

    def test_malformed_pressure_metadata_is_unknown_not_healthy(self):
        for fields in ({'query_status':None},{'query_status':{}},
                {'memory_pressure':{}},{'memory_pressure':{'level':'normal','system_free_percent':float('nan')}}):
            self.assertFalse(decision(self.policy,{**self.report,**fields})['ready'])

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

    def test_wake_capacity_keeps_existing_jobs_and_reserves_index_slot(self):
        q=runtime.WakeQueue(self.root)
        with patch.object(runtime,'MAX_WAKES',3,create=True):
            first=q.put('learning','/synthetic/a',now=0)
            q.put('learning','/synthetic/b',now=0)
            self.assertIsNone(q.put('learning','/synthetic/c',now=0))
            self.assertEqual(q.put('learning','/synthetic/a',now=0),first)
            self.assertIsNotNone(q.put('index','',now=0))
            self.assertEqual(len(q.pending()),3)

    def test_oversized_wake_target_is_rejected_without_a_new_row(self):
        q=runtime.WakeQueue(self.root)
        with self.assertRaises(ValueError):q.put('learning','/'+('x'*4096))
        self.assertEqual(q.pending(),[])

if __name__=='__main__':unittest.main()
