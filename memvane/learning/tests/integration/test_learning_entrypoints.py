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
from memvane.learning.context_learning.local_cycle import ResourceDeferred
from memvane.runtime import api as runtime
from tests.support.case import FixtureCase
class EntryPoints(FixtureCase):
    """Learning entry points meet runtime admission first; its default coordinator
    lives under this test's own HOME, and no resource sample means deferral."""
    def setUp(self):
        super().setUp()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('os.cpu_count',return_value=18))
        self.stack.enter_context(patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_PROVIDER_COORD_FD':''}))
        name=self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root=Path(name).resolve();self.root.chmod(0o700)
        self.report={'ready':True,'observed_at_mono':time.monotonic(),
            'total_mib':32000,'available_mib':25000,'load_per_cpu':.1,
            'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'level':'normal','system_free_percent':75}}
    def admission(self):
        self.stack.enter_context(patch('memvane.runtime.api.resource_report',return_value=self.report))
        return runtime
    def test_general_teacher_deferral_precedes_scope_and_reservation(self):
        a=self.admission()
        from memvane.learning.context_learning import general_teacher as teacher
        with patch('memvane.runtime.api.resource_report',return_value=None),patch.object(teacher,'_policy') as policy:
            result=teacher.process_pending(self.root,self.root,{})
        self.assertEqual(result['status'],'deferred_resource');policy.assert_not_called()
    def test_scope_inventory_defers_before_scanning_installations(self):
        a=self.admission();from memvane.learning.context_learning import project_scope as scope
        with patch('memvane.runtime.api.resource_report',return_value=None),patch.object(scope,'_envelope') as read:
            with self.assertRaises(ResourceDeferred):scope.require(self.root,{},'local_train',fresh_inventory=True)
        read.assert_not_called()
    def test_automatic_training_defers_before_planning_or_hashing(self):
        a=self.admission();from memvane.learning.context_learning import automatic_staging as stage
        with patch('memvane.runtime.api.resource_report',return_value=None),patch.object(stage,'training_and_validation') as rows:
            result=stage.train_due_staged(self.root,{},{} )
        self.assertEqual(result['status'],'deferred_resource');rows.assert_not_called()
        import json
        self.assertFalse(json.loads((self.root/'resource-defer.json').read_text())['completionAck'])
    def test_cohort_preparation_defers_before_reading_all_history(self):
        a=self.admission();from memvane.learning.context_learning import general_cohort as cohort
        db=Mock();db.execute.side_effect=AssertionError('history read before admission')
        with patch('memvane.runtime.api.resource_report',return_value=None):
            with self.assertRaises(ResourceDeferred):cohort.admitted(db,self.root,{'projectRoot':str(self.root),'cohort':{'id':'fixture','notBeforeUnixMs':1}})
        db.execute.assert_not_called()
    def test_budgeted_manual_teacher_defers_without_a_reservation(self):
        a=self.admission();from memvane.learning.context_learning import teacher_budget as budget
        with patch('memvane.runtime.api.resource_report',return_value=None),patch.object(budget,'reserve_review') as reserve:
            result=budget.budgeted_review_request(self.root,'case','attempt',{})
        self.assertEqual(result['status'],'deferred_resource');reserve.assert_not_called()
    def test_mac_probes_share_the_remaining_observation_deadline(self):
        from memvane.learning.context_learning import shared
        clock=[100.];timeouts=[]
        def probe(argv,**options):
            timeouts.append(options['timeout']);clock[0]+=options['timeout']
            raise subprocess.TimeoutExpired(argv,options['timeout'])
        with patch.object(shared.sys,'platform','darwin'),patch.object(shared.time,'monotonic',side_effect=lambda:clock[0]),patch.object(Path,'is_file',return_value=False),patch.object(shared.subprocess,'run',side_effect=probe):
            report=shared.resource_report({'maxRssMib':512,'minAvailableMib':768,'maxLoadPerCpu':1},detailed=True,deadline=100.3)
        self.assertFalse(report['ready']);self.assertEqual(len(timeouts),1)
        self.assertAlmostEqual(timeouts[0],.3)
    def test_final_resource_rejection_is_a_proved_pre_send_receipt(self):
        a=self.admission()
        from memvane.learning.tests.integration import test_context_learning_general_teacher as fixture
        case=fixture.Checks();case.setUp();self.addCleanup(case.tearDown);case.case()
        def invoke(*args,**options):
            with patch('memvane.runtime.api.resource_report',return_value=None):
                return a.provider_popen(['/synthetic/provider'],before_launch=options['before_launch'],defer_launch=options['defer_launch'])
        with patch('subprocess.Popen',side_effect=AssertionError('must not send')):
            result=case.run_queue(invoke)
        self.assertEqual(result['status'],'deferred_resource')
        with case.db() as db:
            self.assertEqual(db.execute('SELECT stage,error_code FROM project_gold_attempts').fetchone(),('pre_send_rejected','resource_deferred'))
            self.assertEqual(db.execute('SELECT status FROM cases').fetchone()[0],'pending_review')



if __name__ == '__main__':
    unittest.main()
