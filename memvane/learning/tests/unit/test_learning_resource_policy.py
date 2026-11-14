"""Synthetic platform/resource-policy checks; no OS probes, ML or child jobs."""
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
from memvane.learning.context_learning.local_cycle import ResourceDeferred
class MemoryBudget(unittest.TestCase):
    def setUp(self):
        self.profile={'device':'mps','fixedMib':2897,'perPairMib':312,
            'estimateSource':'historical short synthetic MPS envelope',
            'modelIdentitySha256':'a'*64,'systemReserveMib':1024,
            'reserveSource':'synthetic fixture only, not a host recommendation',
            'uncertainty':'old adapter and narrow input shape',
            'maxVerifiedMicrobatchSize':1}
        self.policy={'minAvailableMib':768,'maxRssMib':3584,'maxLoadPerCpu':1,
                     'adaptiveMemory':self.profile}
        self.report={'ready':True,'observed_at_mono':10.,'total_mib':24576,
            'available_mib':6436,'load_per_cpu':.1,'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'level':'normal','system_free_percent':53}}
    def test_running_watch_invalid_reserve_is_unknown_and_psi_stays_distinct(self):
        from memvane.learning.context_learning.training_watch import Watch
        for reserve in (None,'unknown',float('nan')):
            report={**self.report,'memory_admission':{'reserve_mib':reserve}}
            self.assertEqual(Watch(clock=lambda:10.).observe(report)['action'],'kill')
        report={**self.report,'memory_pressure':{'level':'normal','psi_full_avg10':0.}}
        self.assertEqual(Watch(clock=lambda:10.).observe(report)['action'],'continue')
        self.assertEqual(Watch(clock=lambda:10.).observe({**report,'memory_pressure':{'level':'normal'}})['action'],'kill')
    def test_running_watch_does_not_force_yield_below_soft_ratio_target(self):
        from memvane.learning.context_learning.training_watch import Watch
        watch=Watch(clock=lambda:10.,min_available_mib=1024)
        self.assertEqual(watch.observe({**self.report,'total_mib':131072})['action'],'continue')
        self.assertEqual(watch.observe({**self.report,'available_mib':900})['action'],'yield')


if __name__ == '__main__':
    unittest.main()
