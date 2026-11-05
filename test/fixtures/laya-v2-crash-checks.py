"""Exercise v2 recovery with a killed owner and separate snapshot readers."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'test/fixtures/laya-reader-upgrade-proof-checks.py'
OWNER = '''import importlib.util,unittest
s=importlib.util.spec_from_file_location('upgrade',%r)
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner().run(unittest.TestSuite([m.UpgradeProofChecks('test_v2_retarget_preserves_reader_during_every_recovery_step')]))
raise SystemExit(not r.wasSuccessful())''' % str(FIXTURE)
OWNER_RESTORE = OWNER.replace('test_v2_retarget_preserves_reader_during_every_recovery_step',
                              'test_v1_restore_keeps_reader_during_route_reversal')
READER = '''import importlib.util,json,sys,time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root=Path(sys.argv[1]); p=json.loads(sys.stdin.read())
sys.path.insert(0,str(root/'core'))
s=importlib.util.spec_from_file_location('base',root/'test/fixtures/laya-journal-reader-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
import laya_journal_reader as reader,laya_reader_lease as lease,laya_active_upgrade as upgrade
import laya_reader_open as opens,laya_v6_reader as search
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=m.JournalReaderChecks('runTest')
def lookup(root):return next(x for x in p['targets'] if x['pluginRoot']==str(root))
with helper.full_authority(case,reader,lease), \\
     mock.patch.object(upgrade,'inspect_full_plugin_candidate',side_effect=lookup), \\
     mock.patch.object(upgrade,'validate_production_provider_attestation'), \\
     mock.patch.object(upgrade,'_provider_file_identity',side_effect=lambda att:[[att['providerManifest']['providerPath'],1,2,3,4,5]]), \\
     mock.patch.object(reader.journal,'inspect_full_plugin_candidate',side_effect=lookup), \\
     mock.patch.object(reader.journal,'_executing_from_candidate'), \\
     mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:lookup(identity['pluginRoot'])):
    with lease.resolve_endpoint(Path(p['project']),action='recall',host_root=Path(p['host'])) as endpoint:
        assert endpoint.endpoint_id==p['endpoint']
        with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
            result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',decision=p['decision'],provider=None,deadline=time.monotonic()+10)
            assert any('otter' in row['body'] for row in result.rows)
print('reader-ok',flush=True)'''
RESUME = '''import hashlib,importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root=Path(sys.argv[1]);p=json.loads(sys.stdin.read())
sys.path.insert(0,str(root/'core'))
s=importlib.util.spec_from_file_location('base',root/'test/fixtures/laya-journal-reader-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
import laya_active_upgrade as upgrade,laya_journal_reader as reader,laya_reader_lease as lease
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=m.JournalReaderChecks('runTest')
project,state=Path(p['project']),Path(p['state'])
intent=upgrade._load_intent(state,project)
old_att=json.loads(intent['oldAttestation'])
def lookup(path):return next(x for x in p['targets'] if x['pluginRoot']==str(path))
def attest(proof):return {**old_att,'sourceCommit':proof['sourceCommit'],
    'bundleManifestSha256':proof['bundleManifestSha256'],
    'ownershipManifestSha256':proof['ownershipManifestSha256'],
    'providerManifest':{**old_att['providerManifest'],'providerPath':proof['pluginRoot']+'/p.py'}}
def identify(att):return [[att['providerManifest']['providerPath'],1,2,3,4,5]]
def review(**kw):return {'proposalSha256':lookup(kw['plugin_root'])['sourceCommit'][0]*64,
    'reviewedProvider':{'vectorProfile':case.proof['vectorProfile']}}
target=p['targets'][p['resumeTarget']];plugin=Path(target['pluginRoot'])
marker=upgrade._intent_ledger(intent).paths(project,state/'install-journal.sqlite')[1]
with helper.full_authority(case,reader,lease), \\
     mock.patch.object(upgrade,'review_local_provider',side_effect=review), \\
     mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':case.proof['vectorProfile']}), \\
     mock.patch.object(upgrade,'adopt_host_loaded_plugin',side_effect=lambda _source,path,**kw:lookup(path)), \\
     mock.patch.object(upgrade,'inspect_full_plugin_candidate',side_effect=lookup), \\
     mock.patch.object(upgrade,'attestation_record',side_effect=lambda **kw:attest(kw['proof'])), \\
     mock.patch.object(upgrade,'validate_production_provider_attestation'), \\
     mock.patch.object(upgrade,'_provider_file_identity',side_effect=identify), \\
     mock.patch.object(upgrade,'_wait_for_sockets'), \\
     mock.patch.object(reader.journal,'inspect_full_plugin_candidate',side_effect=lookup), \\
     mock.patch.object(reader.journal,'_executing_from_candidate'), \\
     mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:lookup(identity['pluginRoot'])), \\
     mock.patch.object(reader.journal,'_bind_attestation',side_effect=lambda path,identity,proof:(
         hashlib.sha256(reader.journal._canon(attest(proof)).encode()).hexdigest(),identify(attest(proof)))), \\
     upgrade._install_lock(state),upgrade._operation_lock(marker),upgrade._locked_provider():
    result=upgrade._recover_upgrade(intent,state.parent,plugin,target['sourceCommit'],target['sourceCommit'][0]*64,'/synthetic/model',3)
    assert result['status']=='active_upgrade_complete', result
    selection=reader.read_selection(state/'install-journal.sqlite')
    assert selection.preferred_endpoint==p['endpoint']
    journal=reader.journal.inspect_journal(state/'install-journal.sqlite')
    assert journal['state']=='active' and journal['selected']['pluginRoot']==str(plugin)
print('resume-ok',flush=True)'''
RESTORE = '''import hashlib,importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root=Path(sys.argv[1]);p=json.loads(sys.stdin.read());sys.path.insert(0,str(root/'core'))
s=importlib.util.spec_from_file_location('base',root/'test/fixtures/laya-journal-reader-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
import laya_active_upgrade as upgrade,laya_journal_reader as reader,laya_reader_lease as lease
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=m.JournalReaderChecks('runTest');project,state=Path(p['project']),Path(p['state'])
intent=upgrade._load_intent(state,project);target=p['targets'][0]
old_att=json.loads(intent['oldAttestation']);new_att=json.loads(intent['newAttestation'])
def lookup(path):return case.proof if str(path)==str(case.plugin) else target
def identify(att):return [[att['providerManifest']['providerPath'],1,2,3,4,5]]
marker=upgrade._intent_ledger(intent).paths(project,state/'install-journal.sqlite')[1]
with helper.full_authority(case,reader,lease), \\
     mock.patch.object(upgrade,'inspect_full_plugin_candidate',side_effect=lookup), \\
     mock.patch.object(upgrade,'validate_production_provider_attestation'), \\
     mock.patch.object(upgrade,'_provider_file_identity',side_effect=identify), \\
     mock.patch.object(upgrade,'_wait_for_sockets'), \\
     mock.patch.object(reader.journal,'inspect_full_plugin_candidate',side_effect=lookup), \\
     mock.patch.object(reader.journal,'_executing_from_candidate'), \\
     mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:lookup(identity['pluginRoot'])), \\
     mock.patch.object(reader.journal,'_bind_attestation',side_effect=lambda path,identity,proof:(
         hashlib.sha256(reader.journal._canon(new_att if proof==target else old_att).encode()).hexdigest(),
         identify(new_att if proof==target else old_att))), \\
     upgrade._install_lock(state),upgrade._operation_lock(marker),upgrade._locked_provider():
    result=upgrade._restore(intent)
    assert result['status']=='active_upgrade_restored_laya', result
    selection=reader.read_selection(state/'install-journal.sqlite')
    assert selection.preferred_endpoint==p['endpoint']
    journal=reader.journal.inspect_journal(state/'install-journal.sqlite')
    assert journal['state']=='restored_old' and journal['selected']['pluginRoot']==str(case.plugin)
print('resume-ok',flush=True)'''


class RecoveryCrashChecks(unittest.TestCase):
    def test_v2_owner_kill_preserves_reader(self):
        restoring = os.environ.get('MEMVANE_TEST_CRASH_KIND') == 'restore'
        default = ('fence,file-2,file-0,file-1,journal,file-4,ledger' if restoring else
            'normalize-descriptor,normalize-route,switch-descriptor,cas,metadata-0,metadata-1,metadata-2,metadata-3,metadata-4,activate')
        phases = os.environ.get('MEMVANE_TEST_V2_PHASES', default).split(',')
        for phase in phases:
            with self.subTest(phase=phase):
                env = {**os.environ, ('MEMVANE_TEST_RESTORE_CRASH_STAGE' if restoring else
                                     'MEMVANE_TEST_V2_CRASH_STEP'): phase}
                owner = subprocess.Popen([sys.executable, '-B', '-c', OWNER_RESTORE if restoring else OWNER], cwd=ROOT, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 70
                    pending = b''
                    checkpoint = None
                    while time.monotonic() < deadline and checkpoint is None:
                        ready, _, _ = select.select([owner.stdout], [], [], 1)
                        if not ready:
                            self.assertIsNone(owner.poll(), owner.stderr.read().decode() if owner.poll() is not None else '')
                            continue
                        chunk = os.read(owner.stdout.fileno(), 65536)
                        if not chunk:
                            self.fail('owner exited before checkpoint: ' + owner.stderr.read().decode())
                        pending += chunk
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            try:
                                item = json.loads(line)
                            except ValueError:
                                continue
                            if item.get('v2CrashCheckpoint') == phase:
                                checkpoint = item
                    self.assertIsNotNone(checkpoint, 'owner checkpoint timeout: ' + phase)
                    self.assertIsNone(owner.poll())
                    reader_env = {**env, 'HOME': str(Path(checkpoint['state']).parent),
                                  'MEMVANE_TEST_RUNTIME_ROOT': checkpoint['runtimeRoot'],
                                  'QMD_SANDBOX': '1'}
                    def read():
                        return subprocess.run([sys.executable, '-B', '-c', READER, str(ROOT)],
                            input=json.dumps(checkpoint), text=True, capture_output=True,
                            cwd=ROOT, env=reader_env, timeout=30)
                    before = read()
                    self.assertEqual(before.returncode, 0, before.stderr)
                    self.assertIn('reader-ok', before.stdout)
                    self.assertIsNone(owner.poll())
                finally:
                    if owner.poll() is None:
                        owner.kill()
                    owner.wait(timeout=10)
                    owner.stdout.close()
                    owner.stderr.close()
                self.assertEqual(owner.returncode, -9)
                after = read()
                self.assertEqual(after.returncode, 0, after.stderr)
                self.assertIn('reader-ok', after.stdout)
                resumed = subprocess.run([sys.executable, '-B', '-c', RESTORE if restoring else RESUME, str(ROOT)],
                    input=json.dumps(checkpoint), text=True, capture_output=True,
                    cwd=ROOT, env=reader_env, timeout=60)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertIn('resume-ok', resumed.stdout)
                print(json.dumps({'phase': phase, 'ownerExit': owner.returncode,
                                  'concurrentReader': before.returncode,
                                  'afterKillReader': after.returncode,
                                  'resumeOwner': resumed.returncode,
                                  'actualHostExecuted': False}), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
