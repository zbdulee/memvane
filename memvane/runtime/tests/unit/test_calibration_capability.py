import fcntl
import hashlib
import json
import os
import sys
import types
from unittest.mock import patch
from unittest.mock import Mock
from tests.support.case import FixtureCase
from memvane.runtime import bounded_calibration as calibration


class GuardCapability(FixtureCase):
    def setUp(self):
        super().setUp()
        self.m=calibration; calibration.reset_test_state()
        self.root=self.tmp / "state"; self.root.mkdir(mode=0o700)
        (self.root / "calibrations").mkdir(mode=0o700)
        (self.root / "out").mkdir(mode=0o700)
        profile={"device":"mps","fixedMib":2897,"perPairMib":312,"modelIdentitySha256":"a"*64,
                 "estimateSource":"synthetic profile","systemReserveMib":1024,"reserveSource":"synthetic",
                 "maxVerifiedMicrobatchSize":1}
        base={"minAvailableMib":1024,"maxRssMib":3584,"maxLoadPerCpu":.8,"adaptiveMemory":profile}
        request={"schema_version":1,"artifact_path":str(self.root / "out" / "head.artifact"),
          "model_input_view":"tool-edit-8192-choice-v1","train":[{"request_id":"synthetic-one-step-cobalt",
          "input_sha256":"b"*64,"prompt":"What is the synthetic Cobalt beacon code?","candidates":[{"id":"cobalt",
          "input_kind":"full_compact_wiki_body","input_text":"The synthetic Cobalt beacon code is COBALT-271."},{"id":"garden",
          "input_kind":"full_compact_wiki_body","input_text":"The synthetic garden marker is GARDEN-909."}],"selected_ids":["cobalt"]}]}
        def row(t):
            return {"observed_at_mono":t,"ready":True,"available_mib":4506,"total_mib":24576,
                    "load_per_cpu":.2,"swap_used_mib":1000.,"query_status":{"memory_pressure":"ok"},
                    "memory_pressure":{"level":"warning","system_free_percent":38}}
        with patch.object(calibration,"capability",return_value=None):
            self.value=calibration.create([row(t) for t in (10.,13.,16.)],base,request,command=["node","--test","/synthetic/one.test.mjs"],files={},now=16.)
        self.policy={**base,"calibration":self.value};self.request=request
        self.path=self.root / "envelope.json";self.write(self.path,self.value)
        self.claim=self.root / "calibrations" / (self.value["runId"]+".json")
        self.write(self.claim,{"run":"owned-run","nonce":"owned-nonce","envelopePath":str(self.path),"envelopeSha256":self.m._sha(self.value)})
        self.write(self.root / "active.json",{"run":"owned-run","calibrationRunId":self.value["runId"],"calibrationNonce":"owned-nonce","settled":False,"status":"running"})
        self.fd=os.open(self.root / "calibrations" / (self.value["runId"]+".lock"),os.O_CREAT|os.O_RDWR,0o600)
        self.addCleanup(lambda:os.close(self.fd));fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for stub in (patch.object(self.m,"claim_root",return_value=self.root),patch.object(self.m.time,"monotonic",return_value=17.),patch.dict(os.environ,{"QMD_CALIBRATION_ENVELOPE":str(self.path)})):
            stub.start();self.addCleanup(stub.stop)

    @staticmethod
    def write(path,value):path.write_text(json.dumps(value));path.chmod(0o600)


    def test_live_matching_guard_is_required(self):
        self.m.capability(self.policy)
        fcntl.flock(self.fd,fcntl.LOCK_UN)
        with self.assertRaises(ValueError):self.m.capability(self.policy)


    def test_embedded_permission_or_finished_guard_is_insufficient(self):
        with patch.dict(os.environ,{'QMD_CALIBRATION_ENVELOPE':''}):
            with self.assertRaises(ValueError):self.m.capability(self.policy)
        self.write(self.root/'active.json',{'status':'passed','settled':True})
        with self.assertRaises(ValueError):self.m.capability(self.policy)


    def test_same_scope_cannot_launch_a_second_model(self):
        request={**self.value['request'],'learning_control':{'policy':self.policy}}
        self.m.reserve_launch(request)
        with self.assertRaisesRegex(ValueError,'already_launched'):self.m.reserve_launch(request)

    def test_guard_establishes_and_retires_capability_and_refuses_replay(self):
        from scripts import development_guard as guard
        runtime = self.root / 'guard'
        runtime.mkdir(mode=0o700)
        files = {}
        for name in ('one-step.test.mjs', 'one-step.py', 'observed-current-adapter.py'):
            source = self.root / name
            source.write_text('fixed synthetic source')
            source.chmod(0o600)
            files[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
        command = ['node', '--test', '--test-concurrency=1', str(self.root / 'one-step.test.mjs')]
        value = {**self.value, 'command': command, 'files': files}
        self.write(self.path, value)
        policy = {**self.policy, 'calibration': value}
        row = {'ready': True, 'observed_at_mono': 17., 'available_mib': 4506,
               'total_mib': 24576, 'load_per_cpu': .2, 'swap_used_mib': 1000.,
               'query_status': {'memory_pressure': 'ok'},
               'memory_pressure': {'level': 'warning', 'system_free_percent': 38}}
        system = {'total_bytes': 24576 * 1024 ** 2, 'available_percent': 38,
                  'swap_bytes': 1000 * 1024 ** 2, 'load_per_cpu': .2,
                  'cpu_count': 18, 'pressure': 'warning'}
        child = Mock(pid=888888)
        child.poll.return_value = 0
        tree = Mock()
        tree.observe.return_value = {}
        tree.established = True
        def spawn(*args, **kwargs):
            self.m.capability(policy)
            return child
        shared = types.ModuleType('context_learning.shared')
        shared.resource_report = lambda *_args, **_kwargs: row
        args = types.SimpleNamespace(command=command, kind='test', max_seconds=30,
                                     sample_seconds=2, calibration_envelope=str(self.path))
        for name in ('execution_hold', 'system_sample', 'processes', 'OwnedTree'):
            self.assertTrue(hasattr(guard, name), name)
        with patch.dict(sys.modules, {'context_learning.bounded_calibration': self.m,
                                      'context_learning.shared': shared}), \
                patch.object(self.m, 'claim_root', return_value=runtime), \
                patch.dict(vars(guard), {'execution_hold': lambda: False,
                                         'system_sample': lambda: system,
                                         'processes': lambda: [],
                                         'OwnedTree': lambda _pid: tree}), \
                patch.object(guard.subprocess, 'Popen', side_effect=spawn) as launch:
            self.assertEqual(guard.run_job(args, runtime), 0)
            with self.assertRaises(ValueError):
                self.m.capability(policy)
            self.assertEqual(guard.run_job(args, runtime), 75)
            self.assertEqual(launch.call_count, 1)
