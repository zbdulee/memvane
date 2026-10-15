"""Real cache/manifest/registry checks; stub prediction transport, no model import."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path.cwd()
MARKET = 'qmd-auto-context-marketplace'
PLUGIN = 'qmd-auto-context@' + MARKET
OLD, NEW = '1.0.0', '1.0.1'

DRIVER = '''import json,sys,time
from pathlib import Path
sys.dont_write_bytecode=True
root=Path(sys.argv[1]);state=Path(sys.argv[2]);sys.path.insert(0,str(root/'core'))
from context_learning import live_select, inference_service
from context_learning.local_cycle import VerifiedLayaTrainer
from context_learning.contracts import digest
calls=[]
def admission(self,*,cwd):
    assert self.argv[1]==str(root/'core/context_learning/laya_adapter.py'),self.argv
    assert self.argv[0]==sys.executable and self.argv[2]==sys.argv[3]
    assert self.identity and self.proof is None
    return {}
def prediction(state,policy,request,artifact_sha,**kwargs):
    calls.append('predict')
    case=request['cases'][0]
    assert case['candidates'][0]['input_text']=='Synthetic complete body.\\n'
    return {'schema_version':1,'predictions':[{'request_id':case['request_id'],
        'input_sha256':case['input_sha256'],'selected_ids':['wiki/synthetic.md']}]},{}
VerifiedLayaTrainer.validate_prediction=admission
inference_service.predict=prediction
text='---\\ntitle: Synthetic\\nstatus: verified\\n---\\nSynthetic complete body.\\n'
started=time.monotonic()
deadline=started+float(sys.argv[4]) if sys.argv[4]!='none' else None
ids,reason,_=live_select.choose(state,'Synthetic request',[{'id':'wiki/synthetic.md',
    'revision_sha256':digest(text),'source_text':text}],corpus_fingerprint='a'*64,deadline=deadline)
assert 'torch' not in sys.modules and 'laya' not in sys.modules
print(json.dumps({'ids':ids,'reason':reason,'predictionTransports':len(calls),'elapsed':time.monotonic()-started}))
'''

def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


with tempfile.TemporaryDirectory(prefix='qmd-adapter-binding-') as temporary:
    base = Path(temporary).resolve()
    home = base / 'User With Spaces'; home.mkdir(mode=0o700)
    roots = {}
    for host in ('claude', 'codex'):
        for version in (OLD, NEW):
            root = home / ('.'+host) / 'plugins/cache' / MARKET / 'qmd-auto-context' / version
            root.mkdir(parents=True)
            shutil.copytree(SOURCE/'core', root/'core',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            for directory in ('.claude-plugin', '.codex-plugin'):
                private(root/directory/'plugin.json', {'name':'qmd-auto-context','version':version})
            private(root/'package.json', {'name':'qmd-auto-context','version':version})
            roots[host,version] = root

    claude_inventory = home / '.claude/plugins/installed_plugins.json'
    claude_known = home / '.claude/plugins/known_marketplaces.json'
    installed = {'version':2,'plugins':{PLUGIN:[{'scope':'user','version':NEW,
        'installPath':str(roots['claude',NEW]),'gitCommitSha':'a'*40}]}}
    known = {MARKET:{'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}}}
    private(claude_inventory, installed); private(claude_known, known)
    inventory_file = home/'codex-inventory.json'
    codex_inventory = {'installed':[{'pluginId':PLUGIN,'name':'qmd-auto-context',
        'marketplaceName':MARKET,'version':NEW,'installed':True,'enabled':True,
        'source':{'source':'git','url':'https://github.com/zbdulee/qmd-auto-context.git'}}]}
    private(inventory_file, codex_inventory)
    binary = home/'bin/codex'; binary.parent.mkdir()
    binary.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
        'assert "OPENAI_API_KEY" not in os.environ and "ANTHROPIC_API_KEY" not in os.environ\n'
        'assert sys.argv[1:]==["plugin","list","--marketplace","qmd-auto-context-marketplace","--json"]\n'
        'print(open('+repr(str(inventory_file))+').read())\n')
    binary.chmod(0o700)
    env = {**os.environ,'HOME':str(home),'CODEX_HOME':str(home/'.codex'),
        'PATH':str(binary.parent)+os.pathsep+os.environ['PATH'],
        'SYNTHETIC_CODEX_INVENTORY':str(inventory_file),
        'PYTHONDONTWRITEBYTECODE':'1','QMD_RECALL_LOG':''}
    model = home/'local model'; model.mkdir()
    for name in ('model.safetensors','rl_agent_config.json','encoder/config.json',
                 'tokenizer/tokenizer.json','tokenizer/tokenizer_config.json'):
        file=model/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'synthetic bytes')
    state = home/'private state';state.mkdir(mode=0o700)
    artifact=state/'head.artifact';artifact.write_bytes(b'synthetic preserved checkpoint');artifact.chmod(0o600)
    private(state/'active-checkpoint.json',{'schema_version':1,'artifact_path':str(artifact),
        'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()})
    policy={'schema':'qmd-live-selector-v1','trainerArgv':[sys.executable,
        str(roots['codex',OLD]/'core/context_learning/laya_adapter.py'),str(model)],
        'timeoutSeconds':5,'maxRssMib':512}
    private(state/'live-selector.json',policy)
    constructor_calls=0

    def invoke(host, expected='selected', *, budget=None):
        global constructor_calls
        before={p.name:p.read_bytes() for p in state.iterdir() if p.is_file()}
        inode=state.stat().st_ino
        report=json.loads(subprocess.check_output([sys.executable,'-c',DRIVER,
            str(roots[host,NEW]),str(state),str(model),str(budget) if budget is not None else 'none'],env=env,text=True,timeout=10))
        assert report['reason']==expected,(host,report,expected)
        assert state.stat().st_ino==inode
        assert {p.name:p.read_bytes() for p in state.iterdir() if p.is_file()}==before
        if expected=='selected':
            assert report['ids']==['wiki/synthetic.md'] and report['predictionTransports']==1
            constructor_calls+=1
        else:
            assert report['ids'] is None and report['predictionTransports']==0
        return report

    # Both previously approved host anchors may be reused by either current
    # host, but execution must enter the current host's actual trainer class.
    for old_host in ('codex','claude'):
        policy['trainerArgv'][1]=str(roots[old_host,OLD]/'core/context_learning/laya_adapter.py')
        private(state/'live-selector.json',policy)
        for host in ('claude','codex'):invoke(host)

    old_adapter=roots['codex',OLD]/'core/context_learning/laya_adapter.py'
    policy['trainerArgv'][1]=str(old_adapter);private(state/'live-selector.json',policy)
    old_bytes=old_adapter.read_bytes()
    old_adapter.write_bytes(old_bytes+b'\n# different adapter\n')
    for host in ('claude','codex'):invoke(host,'unsupported_adapter')
    old_adapter.write_bytes(old_bytes)

    # A byte-identical file outside the official cache is not an approved anchor.
    rogue=home/'arbitrary adapter.py';rogue.write_bytes(old_bytes)
    policy['trainerArgv'][1]=str(rogue);private(state/'live-selector.json',policy)
    for host in ('claude','codex'):invoke(host,'unsupported_adapter')
    policy['trainerArgv'][1]=str(old_adapter);private(state/'live-selector.json',policy)

    old_adapter.unlink();old_adapter.symlink_to(roots['codex',NEW]/'core/context_learning/laya_adapter.py')
    for host in ('claude','codex'):invoke(host,'unsupported_adapter')
    old_adapter.unlink();old_adapter.write_bytes(old_bytes)
    old_adapter.chmod(0o666)
    for host in ('claude','codex'):invoke(host,'unsupported_adapter')
    old_adapter.chmod(0o644)
    old_adapter.parent.chmod(0o777)
    for host in ('claude','codex'):invoke(host,'unsupported_adapter')
    old_adapter.parent.chmod(0o755)
    old_adapter.unlink()
    for host in ('claude','codex'):invoke(host,'unsupported_adapter')
    old_adapter.write_bytes(old_bytes)

    manifest=roots['codex',OLD]/'.codex-plugin/plugin.json'
    original_manifest=manifest.read_bytes()
    private(manifest,{'name':'qmd-auto-context','version':'wrong'})
    for host in ('claude','codex'):invoke(host,'unsupported_adapter')
    manifest.write_bytes(original_manifest)
    current_manifest=roots['claude',NEW]/'.claude-plugin/plugin.json'
    current_bytes=current_manifest.read_bytes()
    private(current_manifest,{'name':'another-plugin','version':NEW})
    invoke('claude','unsupported_adapter');current_manifest.write_bytes(current_bytes)
    current_adapter=roots['codex',NEW]/'core/context_learning/laya_adapter.py'
    current_bytes=current_adapter.read_bytes()
    current_adapter.write_bytes(current_bytes+b'\n# changed current adapter\n')
    invoke('codex','unsupported_adapter');current_adapter.write_bytes(current_bytes)

    private(claude_inventory,{'version':2,'plugins':{}})
    invoke('claude','unsupported_adapter');private(claude_inventory,installed)
    # Normal scopes can share one exact binding; conflicting or malformed
    # records must not let a single matching entry override the ambiguity.
    duplicate=copy.deepcopy(installed)
    duplicate['plugins'][PLUGIN].append({**duplicate['plugins'][PLUGIN][0],'scope':'project'})
    private(claude_inventory,duplicate);invoke('claude')
    duplicate['plugins'][PLUGIN][1]['version']=OLD
    private(claude_inventory,duplicate);invoke('claude','unsupported_adapter')
    duplicate['plugins'][PLUGIN][1]=None
    private(claude_inventory,duplicate);invoke('claude','unsupported_adapter')
    private(claude_inventory,installed)
    bad=copy.deepcopy(codex_inventory);bad['installed'][0]['installed']=False
    private(inventory_file,bad);invoke('codex','unsupported_adapter');private(inventory_file,codex_inventory)
    bad=copy.deepcopy(codex_inventory)
    bad['installed'].append({**bad['installed'][0],
        'source':{'source':'git','url':'https://github.com/someone/else.git'}})
    private(inventory_file,bad);invoke('codex','unsupported_adapter');private(inventory_file,codex_inventory)
    original_cli=binary.read_bytes()
    binary.write_text('#!'+sys.executable+'\nraise SystemExit(7)\n')
    invoke('codex','unsupported_adapter');binary.write_bytes(original_cli)
    binary.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(3)\n')
    invoke('codex','unsupported_adapter');binary.write_bytes(original_cli)
    # Expired/subsecond budgets cannot start inventory; a longer but bounded
    # budget must interrupt a slow CLI before the former fixed two seconds.
    marker=home/'unexpected-inventory-start'
    binary.write_text('#!'+sys.executable+'\nfrom pathlib import Path\n'
        +'Path('+repr(str(marker))+').touch()\n')
    for budget in (-1,0.1):
        report=invoke('codex','timeout',budget=budget)
        assert not marker.exists() and report['elapsed']<0.5,report
    binary.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(3)\n')
    report=invoke('codex','timeout',budget=1.1)
    assert report['elapsed']<1.8,report
    binary.write_bytes(original_cli)
    inventory_file.write_text('{invalid JSON')
    invoke('codex','unsupported_adapter');private(inventory_file,codex_inventory)
    claude_inventory.write_text('{invalid JSON')
    invoke('claude','unsupported_adapter');private(claude_inventory,installed)
    env['CODEX_HOME']=str(home/'different tool root')
    invoke('codex','unsupported_adapter');env['CODEX_HOME']=str(home/'.codex')

    # Compatibility never substitutes another interpreter or model, and the
    # real trainer constructor must still reject unavailable explicit inputs.
    policy['trainerArgv'][0]=str(home/'missing interpreter')
    private(state/'live-selector.json',policy)
    for host in ('claude','codex'):invoke(host,'runtime_unavailable')
    policy['trainerArgv'][0]=sys.executable
    policy['trainerArgv'][2]=str(home/'missing model')
    private(state/'live-selector.json',policy)
    for host in ('claude','codex'):invoke(host,'runtime_unavailable')
    policy['trainerArgv'][2]=str(model)
    private(claude_known,{MARKET:{'source':{'source':'github','repo':'someone/else'}}})
    invoke('claude','unsupported_adapter');private(claude_known,known)
    bad=copy.deepcopy(codex_inventory);bad['installed'][0]['source']['url']='https://github.com/someone/else.git'
    private(inventory_file,bad);invoke('codex','unsupported_adapter');private(inventory_file,codex_inventory)

    for host in ('claude','codex'):
        policy['trainerArgv'][1]=str(roots[host,NEW]/'core/context_learning/laya_adapter.py')
        private(state/'live-selector.json',policy);invoke(host)
    assert constructor_calls==7
    print(json.dumps({'bothHosts':True,'versionPaths':True,'wrongAdapterDenied':True,
        'inventoryRequired':True,'originRequired':True,'manifestRequired':True,
        'unsafePathsDenied':True,'policyAndCheckpointUnchanged':True,
        'deadlineBounded':True,'claudeScopesUnambiguous':True,'realTrainerConstructor':True,'exactPathCompatible':True,
        'modelCalls':0,'providerCalls':0}))
