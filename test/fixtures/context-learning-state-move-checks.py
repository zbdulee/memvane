#!/usr/bin/env python3
"""Learning state move and automatic selector heal, synthetic HOME only.

The move runs in-process against a temporary HOME passed explicitly. The heal
runs in a child process whose HOME is the temporary one and whose code is a
copy of ``core`` inside synthetic official plugin caches; the model, the
synthetic attestation run and inference preparation are stubbed at the two
heal seams (``selector_heal.attest``/``prepare``) and at the prediction
transport, like ``context-learning-adapter-binding-checks.py``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'core'))
from context_learning import state_move  # noqa: E402
from context_learning.runtime_setup import learning_state_base  # noqa: E402

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL = [REAL_HOME / 'Library' / 'Application Support' / name
        for name in ('qmd-auto-context', 'memvane')]


def _mtimes():
    return {str(path): path.stat().st_mtime_ns if path.exists() else None for path in REAL}


BEFORE = _mtimes()


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class Kill(BaseException):
    pass


class LearningMoveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lsm-', dir='/private/tmp')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        support = (('Library', 'Application Support') if sys.platform == 'darwin'
                   else ('.local', 'share'))
        self.legacy = self.home.joinpath(*support, 'qmd-auto-context')
        self.current = self.home.joinpath(*support, 'memvane')
        self.namespace = self.legacy / 'live-selection' / ('a' * 64)
        self.namespace.mkdir(parents=True, mode=0o700)
        for path in (self.legacy, self.legacy / 'live-selection'):
            path.chmod(0o700)
        artifact = self.namespace / 'laya-base-x.artifact'
        artifact.write_bytes(b'head')
        artifact.chmod(0o600)
        private(self.namespace / 'active-checkpoint.json',
                {'schema_version': 1, 'artifact_path': str(artifact),
                 'artifact_sha256': hashlib.sha256(b'head').hexdigest()})
        self.inference = self.legacy / 'laya-inference'
        self.inference.mkdir(mode=0o700)
        for name in ('provider.lock', 'compute.lock', 'runtime-worker.lock'):
            (self.inference / name).write_bytes(b'')
            (self.inference / name).chmod(0o600)
        self.lock_inode = (self.inference / 'provider.lock').stat().st_ino

    def test_move_switches_both_trees_and_rebases_the_checkpoint(self):
        self.assertEqual(learning_state_base(self.home), self.legacy)
        result = state_move.move(self.home, wait=0.2)
        self.assertEqual(result['status'], 'learning_state_moved', result)
        self.assertEqual(learning_state_base(self.home), self.current)
        self.assertFalse((self.legacy / 'live-selection').exists())
        self.assertFalse((self.legacy / 'laya-inference').exists())
        moved = self.current / 'live-selection' / ('a' * 64)
        pointer = json.loads((moved / 'active-checkpoint.json').read_text())
        self.assertEqual(pointer['artifact_path'], str(moved / 'laya-base-x.artifact'))
        # Lane locks keep their inode: a holder from before the move and one
        # from after it exclude each other.
        self.assertEqual((self.current / 'laya-inference' / 'provider.lock').stat().st_ino,
                         self.lock_inode)
        from context_learning.inference_service import service_root
        self.assertEqual(service_root(create=True, home=self.home),
                         self.current / 'laya-inference')
        self.assertEqual(state_move.move(self.home)['status'], 'learning_state_switched')

    def test_a_busy_lane_defers_before_anything_moves(self):
        fd = os.open(self.inference / 'runtime-worker.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(state_move.LearningMoveDeferred, 'learning_lane_busy'):
                state_move.move(self.home, wait=0.2)
        finally:
            os.close(fd)
        self.assertTrue((self.legacy / 'live-selection').is_dir())
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_service_still_holding_its_lease_defers_the_move(self):
        service = self.home / '.local' / 'state' / 'memvane' / 'service'
        service.mkdir(parents=True, mode=0o700)
        for path in (service.parent, service.parent.parent, service.parent.parent.parent):
            path.chmod(0o700)
        (service / 'service.lock').write_bytes(b'')
        (service / 'service.lock').chmod(0o600)
        fd = os.open(service / 'service.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                        'learning_service_draining'):
                state_move.move(self.home, wait=0.3)
        finally:
            os.close(fd)
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_setup_bound_namespace_is_not_moved(self):
        private(self.namespace / state_move.SETUP_BINDINGS, {'phase': 'bound'})
        with self.assertRaisesRegex(state_move.LearningMoveRefused,
                                    'learning_state_move_bound'):
            state_move.move(self.home, wait=0.2)
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_crash_between_the_renames_resumes(self):
        def die(name):
            if name == 'moved:live-selection':
                raise Kill(name)
        with mock.patch.object(state_move, '_step', die):
            with self.assertRaises(Kill):
                state_move.move(self.home, wait=0.2)
        self.assertEqual(state_move.status(self.home)[0], 'pending')
        result = state_move.move(self.home, wait=0.2)
        self.assertEqual(result['status'], 'learning_state_moved')
        self.assertEqual(learning_state_base(self.home), self.current)

    def test_a_service_elected_before_the_move_serves_the_moved_root(self):
        old = str(self.inference)
        state_move.move(self.home, wait=0.2)
        self.assertEqual(state_move.current_root(old, home=self.home),
                         str(self.current / 'laya-inference'))

    def test_a_recall_that_cannot_select_asks_the_worker_to_heal(self):
        import laya_host_response as response
        calls = []
        with mock.patch('laya_bundle_upgrade.request',
                        side_effect=lambda *args: calls.append(args)):
            response._request_selector_heal({'projectRoot': '/p', 'journalPath': '/j'},
                                            'adapter_anchor_missing')
            response._request_selector_heal({'projectRoot': '/p', 'journalPath': '/j'},
                                            'timeout')
        self.assertEqual(calls, [('/p', '/j', 'learning_selector')])


MARKET = 'memvane-marketplace'
PLUGIN = 'memvane@' + MARKET
OLD, NEW = '1.0.0', '1.0.1'
DRIVER = r'''import json,sys,time
from pathlib import Path
sys.dont_write_bytecode=True
root=Path(sys.argv[1]);state=Path(sys.argv[2]);mode=sys.argv[3]
sys.path.insert(0,str(root/'core'))
from context_learning import live_select, inference_service, selector_heal
from context_learning.local_cycle import VerifiedLayaTrainer
from context_learning.contracts import digest
from runtime_lease import atomic_json
calls={'attest':0,'prepare':0}
def admission(self,*,cwd):
    assert self.argv[1]==str(root/'core/context_learning/laya_adapter.py'),self.argv
    return {}
def prediction(state,policy,request,artifact_sha,**kwargs):
    case=request['cases'][0]
    return {'schema_version':1,'predictions':[{'request_id':case['request_id'],
        'input_sha256':case['input_sha256'],'selected_ids':['wiki/synthetic.md']}]},{}
VerifiedLayaTrainer.validate_prediction=admission
inference_service.predict=prediction
prepared=[]
def attest(state,policy):
    calls['attest']+=1
    if mode=='defer':
        from resource_deferred import ResourceDeferred
        raise ResourceDeferred('trainer_adaptive_attestation_required')
    import hashlib
    atomic_json(state/'laya-synthetic-attestation.json',{'schema':'qmd-laya-live-proof-v1',
        'verifiedAt':time.time(),'attestation':{'adapter_sha256':hashlib.sha256(
        Path(policy['trainerArgv'][1]).read_bytes()).hexdigest()}})
def prepare(state,policy):
    calls['prepare']+=1;prepared.append(1)
selector_heal.attest=attest;selector_heal.prepare=prepare
selector_heal.admitted=lambda state,argv:bool(prepared) or mode=='ready'
text='---\ntitle: Synthetic\nstatus: verified\n---\nSynthetic complete body.\n'
def choose():
    return live_select.choose(state,'Synthetic request',[{'id':'wiki/synthetic.md',
        'revision_sha256':digest(text),'source_text':text}],corpus_fingerprint='a'*64)[1]
out={'before':choose()}
try:
    out['heal']=selector_heal.heal(None,state=state)
except Exception as exc:
    out['heal']={'error':str(exc)}
out['after']=choose();out['calls']=calls
out['anchor']=json.loads((state/'live-selector.json').read_text())['trainerArgv'][1]
print(json.dumps(out))
'''


class SelectorHealTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lsh-', dir='/private/tmp')
        self.addCleanup(temp.cleanup)
        home = self.home = Path(temp.name).resolve() / 'User Home'
        home.mkdir(mode=0o700)
        self.roots = {}
        for host in ('claude', 'codex'):
            for version in (OLD, NEW):
                root = home / ('.' + host) / 'plugins/cache' / MARKET / 'memvane' / version
                root.mkdir(parents=True)
                shutil.copytree(SOURCE / 'core', root / 'core',
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                for directory in ('.claude-plugin', '.codex-plugin'):
                    private(root / directory / 'plugin.json', {'name': 'memvane', 'version': version})
                private(root / 'package.json', {'name': 'memvane', 'version': version})
                self.roots[host, version] = root
        private(home / '.claude/plugins/installed_plugins.json',
                {'version': 2, 'plugins': {PLUGIN: [{'scope': 'user', 'version': NEW,
                 'installPath': str(self.roots['claude', NEW]), 'gitCommitSha': 'a' * 40}]}})
        private(home / '.claude/plugins/known_marketplaces.json',
                {MARKET: {'source': {'source': 'github', 'repo': 'zbdulee/memvane'}}})
        self.env = {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(home / '.codex'),
                    'PYTHONDONTWRITEBYTECODE': '1', 'QMD_RECALL_LOG': ''}
        model = home / 'model'
        for name in ('model.safetensors', 'rl_agent_config.json', 'encoder/config.json',
                     'tokenizer/tokenizer.json', 'tokenizer/tokenizer_config.json'):
            file = model / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'synthetic bytes')
        self.state = home / 'state'
        self.state.mkdir(mode=0o700)
        artifact = self.state / 'head.artifact'
        artifact.write_bytes(b'head')
        artifact.chmod(0o600)
        private(self.state / 'active-checkpoint.json',
                {'schema_version': 1, 'artifact_path': str(artifact),
                 'artifact_sha256': hashlib.sha256(b'head').hexdigest()})
        # The anchor names a superseded cache the host already removed.
        gone = home / '.claude/plugins/cache' / MARKET / 'memvane' / '0.9.0'
        self.policy = {'schema': 'qmd-live-selector-v1',
                       'trainerArgv': [sys.executable,
                                       str(gone / 'core/context_learning/laya_adapter.py'),
                                       str(model)],
                       'timeoutSeconds': 5, 'maxRssMib': 512}
        private(self.state / 'live-selector.json', self.policy)

    def drive(self, mode='heal'):
        out = subprocess.check_output([sys.executable, '-c', DRIVER,
                                       str(self.roots['claude', NEW]), str(self.state), mode],
                                      env=self.env, text=True, timeout=60)
        return json.loads(out)

    def test_a_removed_anchor_is_rebound_and_local_selection_returns(self):
        report = self.drive()
        self.assertEqual(report['before'], 'adapter_anchor_missing', report)
        self.assertEqual(report['heal'], {'status': 'selector_prepared', 'rebound': True})
        self.assertEqual(report['anchor'], str(self.roots['claude', NEW] /
                                               'core/context_learning/laya_adapter.py'))
        self.assertEqual(report['after'], 'selected', report)
        self.assertEqual(report['calls'], {'attest': 1, 'prepare': 1})

    def test_ready_evidence_is_left_alone(self):
        report = self.drive('ready')
        self.assertEqual(report['heal'], {'status': 'selector_ready', 'rebound': True})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_an_accepted_learning_profile_needs_the_owner(self):
        private(self.state / 'learning-profile.json', {'proposal': {}})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_profile_rebind_required'})
        self.assertEqual(report['anchor'], self.policy['trainerArgv'][1])
        self.assertEqual(report['after'], 'adapter_anchor_missing')

    def test_a_running_adapter_that_is_not_installed_is_not_adopted(self):
        private(self.home / '.claude/plugins/installed_plugins.json',
                {'version': 2, 'plugins': {}})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'adapter_not_official'})
        self.assertEqual(report['anchor'], self.policy['trainerArgv'][1])

    def test_an_attestation_deferral_is_reported_not_bypassed(self):
        report = self.drive('defer')
        self.assertEqual(report['heal'], {'error': 'trainer_adaptive_attestation_required'})
        self.assertEqual(report['calls'], {'attest': 1, 'prepare': 0})


def tearDownModule():
    if _mtimes() != BEFORE:
        raise AssertionError('real learning state changed: ' + json.dumps(_mtimes()))


if __name__ == '__main__':
    unittest.main()
