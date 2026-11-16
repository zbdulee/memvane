"""Legacy provider upgrade from the actual host hook through the sync owner."""
import json
import hashlib
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
WORKER_103 = '8d6fdd22432d22c3256015da501ff6ef063a899f8c10e198831f1db99509a43c'
sys.path.insert(0, str(ROOT))
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

# Only the provider/runtime boundary is synthetic. The hook, wake queue, journal,
# reattestation comparison/write and sync owner are the production paths.
PATCH = '''
import os
from pathlib import Path
from memvane.provider import fixture_port as f
from memvane.provider.adapters import reattest as r
from memvane.provider import api as p
f.ARTIFACT_SHA = r.LEGACY_PROVIDER
_original_manifest = f.FixtureProvider.manifest
_original_proof = f.FixtureProvider.provider_manifest
_original_port = p.fixture_port
_original_hash = p.hash_file

def manifest(self, root):
    return dict(_original_manifest(self, root), workerSha256=r.LEGACY_WORKER,
                workerPath='/synthetic/laya_local_embed_worker.mjs',
                providerPath='/synthetic/local_embed_provider.py')
def hash_file(path):
    if os.environ.get('MV_REATTEST_SIM'):
        if path.endswith('laya_local_embed_worker.mjs'): return '8d6fdd22432d22c3256015da501ff6ef063a899f8c10e198831f1db99509a43c'
        if path.endswith('local_embed_provider.py'): return r.LEGACY_PROVIDER
    return _original_hash(path)
def current(m):
    identity = {'modelSha256':m['modelSha256'], 'tokenizer':'model-embedded',
                'chunkerSha256':r.LEGACY_CHUNKER, 'chunkRules':'900/135/200',
                'dimension':m['dimension'], 'metric':'cosine', **r.LEGACY_SEMANTICS}
    if os.environ.get('MV_REATTEST_IDENTITY_CHANGED'):
        identity['normalization'] = 'l2'
    return {'worker':'8d6fdd22432d22c3256015da501ff6ef063a899f8c10e198831f1db99509a43c', 'provider':r.LEGACY_PROVIDER}, identity
def proof(self, record, profile, *, allow_local_unavailable=False):
    result = _original_proof(self, record, profile, allow_local_unavailable=allow_local_unavailable)
    if os.environ.get('MV_REATTEST_SIM'):
        manifest = result[0] if allow_local_unavailable else result
        manifest = dict(manifest, workerPath='/synthetic/laya_local_embed_worker.mjs',
                        providerPath='/synthetic/local_embed_provider.py')
        available = r.check(Path(record['journalPath']).parent, manifest)
        if allow_local_unavailable: return manifest, available
        if not available: raise f.RuntimeUnavailable('provider_identity_changed')
        return manifest
    return result
def fixture_port(port=None):
    # Allow the actual sync reattestation owner to run against the fixture port.
    import sys
    if sys._getframe(1).f_code.co_name == '_prepare_provider_reattest': return None
    return _original_port(port)
f.FixtureProvider.manifest = manifest
f.FixtureProvider.provider_manifest = proof
r._current = current
p.fixture_port = fixture_port
p.hash_file = hash_file
'''


class ReattestHook(FixtureCase):
    def test_session_hook_wakes_owner_and_recall_uses_resealed_provider(self):
        worker=ROOT/'memvane/provider/adapters/laya_local_embed_worker.mjs'
        self.assertEqual(hashlib.sha256(worker.read_bytes()).hexdigest(),WORKER_103)
        provider=ROOT/'memvane/provider/adapters/local_embed_provider.py'
        self.assertEqual(hashlib.sha256(provider.read_bytes()).hexdigest(),
                         '87cdbd2b1144c816eaf2ddd71bac3f68c6876c8f1548f29368da05882732e8ee')
        patch_dir = Path(tempfile.mkdtemp(prefix='mvr-', dir=self.home))
        (patch_dir / 'sitecustomize.py').write_text(PATCH)
        # Apply the identical private fixture seam to setup and the owner pass.
        namespace = {}
        exec(compile(PATCH, str(patch_dir/'sitecustomize.py'), 'exec'), namespace)
        project = make_project(self.home, name='reattest')
        note = project.write_note('notes.md', 'Synthetic legacy source.\n')
        project.write_card('concepts/recovery.md', title='recovery beacon',
                           body='recoverybeacon trusted content\n', sources=[note])
        project.commit('fixture')
        install = build_install(self, project)
        original = (install.state/'provider-attestation.json').read_bytes()
        os.environ['MV_REATTEST_SIM'] = '1'
        self.addCleanup(os.environ.pop, 'MV_REATTEST_SIM', None)
        from memvane.runtime.api import wake_queue
        queue = wake_queue()
        self.assertFalse([r for r in queue.pending() if r['kind']=='laya-sync'])
        env = {**os.environ, 'PYTHONPATH':str(patch_dir)+os.pathsep+str(ROOT),
               'QMD_SETUP_GUARD_FIXTURE':'', 'QMD_RUNTIME_DISPATCH':'1',
               'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT':'1', 'PYTHONDONTWRITEBYTECODE':'1'}
        for key in ('QMD_SANDBOX','CODEX_SANDBOX','CLAUDE_SANDBOX','CLAUDE_HEADLESS','GEMINI_SANDBOX'):
            env.pop(key, None)
        payload = {'cwd':str(project.root),'session_id':'fixture','hook_event_name':'SessionStart'}
        incompatible = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'update-queued','claude'],
            input=json.dumps(payload),text=True,capture_output=True,
            env={**env,'MV_REATTEST_IDENTITY_CHANGED':'1'},cwd=project.root,timeout=60)
        self.assertEqual(incompatible.returncode,0,incompatible.stderr)
        self.assertIn('다시 온보딩',json.loads(incompatible.stdout)['hookSpecificOutput']['additionalContext'])
        self.assertFalse([r for r in queue.pending() if r['kind']=='laya-sync'])
        hook = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'update-queued','claude'],
            input=json.dumps(payload),text=True,capture_output=True,env=env,cwd=project.root,timeout=60)
        self.assertEqual(hook.returncode,0,hook.stderr)
        self.assertIn('확인하는 중',json.loads(hook.stdout)['hookSpecificOutput']['additionalContext'])
        wakes = [r for r in queue.pending() if r['kind']=='laya-sync' and r['target']==str(project.root)]
        self.assertEqual(len(wakes),1)
        recall = {'cwd':str(project.root),'session_id':'fixture','hook_event_name':'UserPromptSubmit',
                  'prompt':'recoverybeacon'}
        lexical = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'recall','claude'],
            input=json.dumps(recall),text=True,capture_output=True,env=env,cwd=project.root,timeout=60)
        self.assertEqual(lexical.returncode,0,lexical.stderr)
        self.assertIn('recoverybeacon', lexical.stdout)
        sparse = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'recall','claude'],
            input=json.dumps({**recall,'prompt':'recoverybeacon missingfixtureterm'}),text=True,
            capture_output=True,env=env,cwd=project.root,timeout=60)
        self.assertEqual(sparse.returncode,0,sparse.stderr)
        self.assertIn('recoverybeacon', sparse.stdout)
        self.assertEqual(len([r for r in queue.pending() if r['kind']=='laya-sync' and r['target']==str(project.root)]),1)
        result = install.run_sync()
        self.assertIn(result.get('state'), ('indexed','unchanged','pending'))
        self.assertTrue((install.state/'provider-reattest.json').is_file())
        self.assertEqual((install.state/'provider-attestation.json').read_bytes(),original)
        # Exercise the real resident's route and worker attestation. Only the
        # model process is replaced by the deterministic socket fixture.
        from memvane.resident import resident_service as resident
        from memvane.resident.embed_resident import _IDENTITY
        from memvane.resident.tests.support import Handle
        resident_root = self.home / 'resident-fixture'
        resident_root.mkdir(mode=0o700)
        handles = []
        def start_worker(manifest, admit):
            self.assertTrue(admit())
            process = subprocess.Popen([sys.executable, '-B', '-m', 'memvane.resident.tests.support',
                '--fake-worker', manifest['serviceSocketPath'],
                json.dumps({key:manifest[key] for key in _IDENTITY}), '0.01',
                str(self.home/'pending'), str(self.home/'slow'), str(manifest['dimension'])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            handle = Handle(process, manifest['serviceSocketPath'])
            handles.append(handle)
            self.assertEqual(process.stdout.readline(), b'READY\n')
            return handle
        service = resident.Service(resident_root, bundle={'version':'1.0.3','digest':'fixture'},
                                   start_worker=start_worker, admit=lambda:True)
        try:
            deadline=time.monotonic()+20
            while str(project.root) not in service.bindings and time.monotonic()<deadline:
                service._adopt(str(project.root))
                time.sleep(.05)
            self.assertIn(str(project.root), service.bindings, service.unproved)
            self.assertTrue(service.attested)
            self.assertTrue(any(row.get('event')=='resident.adopt' and row.get('outcome')=='adopted'
                                for row in install.events()))
        finally:
            service._release(str(project.root), reason='starting')
            service.close()
            for handle in handles:
                if handle.process.poll() is None: handle.stop()
                if handle.process.stderr: handle.process.stderr.close()
        with sqlite3.connect(str(install.store_path)) as db:
            before=db.execute('select count(*) from vectors').fetchone()[0]
        another=project.write_note('more.md','Synthetic source after reattestation.\n')
        project.write_card('concepts/second.md',title='second beacon',
                           body='secondbeacon retained content\n',sources=[another])
        project.commit('after reattestation')
        for _ in range(5):
            install.run_sync()
            with sqlite3.connect(str(install.store_path)) as db:
                count=db.execute('select count(*) from vectors').fetchone()[0]
            if count>before:break
        self.assertGreater(count,before)
        payload = {'cwd':str(project.root),'session_id':'fixture','hook_event_name':'UserPromptSubmit',
                   'prompt':'recoverybeacon'}
        hook = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'recall','claude'],
            input=json.dumps(payload),text=True,capture_output=True,env=env,cwd=project.root,timeout=60)
        self.assertEqual(hook.returncode,0,hook.stderr)
        self.assertIn('recoverybeacon',hook.stdout)
        cold = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'recall','claude'],
            input=json.dumps({**payload,'prompt':'recoverybeacon missingfixtureterm'}),
            text=True,capture_output=True,env=env,cwd=project.root,timeout=60)
        self.assertEqual(cold.returncode,0,cold.stderr)
        self.assertIn('recoverybeacon',cold.stdout)


if __name__ == '__main__':
    unittest.main()
