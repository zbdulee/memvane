"""Legacy provider upgrade from the actual host hook through the sync owner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
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

def manifest(self, root):
    return dict(_original_manifest(self, root), workerSha256=r.LEGACY_WORKER)
def current(m):
    identity = {'modelSha256':m['modelSha256'], 'tokenizer':'model-embedded',
                'chunkerSha256':r.LEGACY_CHUNKER, 'chunkRules':'900/135/200',
                'dimension':m['dimension'], 'metric':'cosine', **r.LEGACY_SEMANTICS}
    if os.environ.get('MV_REATTEST_IDENTITY_CHANGED'):
        identity['normalization'] = 'l2'
    return {'worker':'c'*64, 'provider':r.LEGACY_PROVIDER}, identity
def proof(self, record, profile, *, allow_local_unavailable=False):
    result = _original_proof(self, record, profile, allow_local_unavailable=allow_local_unavailable)
    if allow_local_unavailable and os.environ.get('MV_REATTEST_SIM'):
        manifest, _ = result
        original = {'worker':manifest['workerSha256'], 'provider':manifest['providerArtifactSha256']}
        now, identity = current(manifest)
        return manifest, r.valid(Path(record['journalPath']).parent, original, now, identity)
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
'''


class ReattestHook(FixtureCase):
    def test_session_hook_wakes_owner_and_recall_uses_resealed_provider(self):
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
        self.assertEqual(len([r for r in queue.pending() if r['kind']=='laya-sync' and r['target']==str(project.root)]),1)
        result = install.run_sync()
        self.assertIn(result.get('state'), ('indexed','unchanged','pending'))
        self.assertTrue((install.state/'provider-reattest.json').is_file())
        self.assertEqual((install.state/'provider-attestation.json').read_bytes(),original)
        payload = {'cwd':str(project.root),'session_id':'fixture','hook_event_name':'UserPromptSubmit',
                   'prompt':'recoverybeacon'}
        hook = subprocess.run(['bash',str(ROOT/'hooks/run-hook'),'recall','claude'],
            input=json.dumps(payload),text=True,capture_output=True,env=env,cwd=project.root,timeout=60)
        self.assertEqual(hook.returncode,0,hook.stderr)
        self.assertIn('recoverybeacon',hook.stdout)


if __name__ == '__main__':
    unittest.main()
