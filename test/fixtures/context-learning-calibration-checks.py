import copy, sys, unittest, types
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'core'))
from context_learning import calibration

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.r={'total_mib':24576,'available_mib':5500,'load_per_cpu':.25,
                'query_status':dict.fromkeys(['load','available','total','memory_pressure'],'ok'),
                'memory_pressure':{'system_free_percent':45}}
    def policy(self,floor=None):
        return calibration.policy(self.r,reserve_fraction=.10,absolute_floor_mib=floor,
                                  max_rss_mib=4096,max_load_per_cpu=1.5)
    def test_relative_without_unconfigured_harness_floor(self):
        p=self.policy();self.assertEqual(p['minAvailableMib'],2458)
        self.assertTrue(calibration.check(self.r,p)['ready'])
    def test_supplied_floor_never_reduced(self):
        self.assertEqual(self.policy(4096)['minAvailableMib'],4096)
        self.assertEqual(self.policy(8192)['minAvailableMib'],8192)
        self.assertFalse(calibration.check(self.r,self.policy(8192))['ready'])
    def test_running_remaining_reserve_not_plus_loaded_peak(self):
        self.r['available_mib']=2600
        self.assertTrue(calibration.check(self.r,self.policy())['ready'])
        self.r['available_mib']=2457
        self.assertFalse(calibration.check(self.r,self.policy())['ready'])
    def test_pressure_and_unknown_fail_closed(self):
        p=self.policy()
        for key in self.r['query_status']:
            r=copy.deepcopy(self.r);r['query_status'][key]='failed'
            self.assertFalse(calibration.check(r,p)['ready'])
        for pressure in [None,{}, {'system_free_percent':9}, {'system_free_percent':float('nan')}]:
            r={**self.r,'memory_pressure':pressure}
            self.assertFalse(calibration.check(r,p)['ready'])
        for status in [None, [], 'ok']:
            self.assertFalse(calibration.check({**self.r,'query_status':status},p)['ready'])
    def test_bad_capacity_load_and_fraction(self):
        for value in [None,0,-1,float('nan')]:
            with self.assertRaises(ValueError):calibration.policy({**self.r,'total_mib':value},reserve_fraction=.1,absolute_floor_mib=None,max_rss_mib=4096,max_load_per_cpu=1.5)
        for fraction in [0,.09,.51,float('nan')]:
            with self.assertRaises(ValueError):calibration.policy(self.r,reserve_fraction=fraction,absolute_floor_mib=None,max_rss_mib=4096,max_load_per_cpu=1.5)
        for key,value in [('available_mib',None),('available_mib',float('nan')),('load_per_cpu',float('inf')),('load_per_cpu',2)]:
            self.assertFalse(calibration.check({**self.r,key:value},self.policy())['ready'])
    def test_both_single_pairs_measured_before_combined_batch(self):
        pairs=[{'tokens':12},{'tokens':120}]
        self.assertEqual(calibration.groups(pairs),[[pairs[0]],[pairs[1]]])
        self.assertEqual(calibration.groups(pairs,include_combined=True),[[pairs[0]],[pairs[1]],pairs])
        for items in [[],[1],[1,2,3],None]:
            with self.assertRaises(ValueError):calibration.groups(items)
    def test_mps_measurement_failures_defer(self):
        def fake(current=1,driver=2,recommended=100,available=True):
            return types.SimpleNamespace(backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda:available)),
                mps=types.SimpleNamespace(synchronize=lambda:None,current_allocated_memory=lambda:current,
                    driver_allocated_memory=lambda:driver,recommended_max_memory=lambda:recommended))
        self.assertEqual(calibration.mps_snapshot(fake())['mpsDriverBytes'],2)
        self.assertIsNone(calibration.mps_snapshot(fake(available=False))['mpsDriverBytes'])
        for args in [dict(current=None),dict(driver=float('nan')),dict(recommended=0),dict(available=None)]:
            with self.assertRaisesRegex(ValueError,'training_memory_unavailable'):calibration.mps_snapshot(fake(**args))
        m=fake()
        def denied():raise PermissionError('denied')
        m.mps.synchronize=denied
        with self.assertRaisesRegex(ValueError,'training_memory_unavailable'):calibration.mps_snapshot(m)
    def test_reload_cases_never_expose_training_gold(self):
        cases=[{'request_id':'x','input_sha256':'a'*64,'prompt':'synthetic','candidates':[{'id':'x'}],
                'selected_ids':['x'],'teacher_metadata':{'gold':True}}]
        expected=[{k:cases[0][k] for k in ('request_id','input_sha256','prompt','candidates')}]
        self.assertEqual(calibration.prediction_cases(cases),expected)
        self.assertIn('selected_ids',cases[0])

unittest.main()
