"""Real recall/capture/enqueue + real consent/cohort/ledger in a private HOME.

QMD results, OS account-home/resource observations and signed-in Codex inventory/quota
are local external-boundary fixtures. Resource verdicts and kernel locks stay real.
The teacher invoke callback stands only for external inference; it
does not attest native CLI execution or process monitoring. No guard, scope,
source, installation, or process identity function is patched. Hooks only
persist a full-worker request and durable wake; the held dispatcher lease keeps
any owned worker from starting while captures commit.
"""
import fcntl
import json
import os
import pwd
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]

# Test-only OS observation provider, also installed in sanitized worker children.
# The product still evaluates freshness, pressure, memory and load itself.
RESOURCE_OBSERVER = '''
import resource_platform
from unittest.mock import patch
class FixtureObserver:
    level = 'normal'
    calls = 0
    def observe(self, *, clock, **options):
        self.calls += 1
        return {'observed_at_mono': clock(), 'load_per_cpu': .1,
            'available_mib': 12000, 'total_mib': 16384, 'swap_used_mib': 0,
            'memory_pressure': {'level': self.level, 'system_free_percent': 70},
            'query_status': {'load': 'ok', 'available': 'ok', 'total': 'ok',
                'memory_pressure': 'ok'}, 'platform': {'scope': 'synthetic'}}
resource_observer = FixtureObserver()
resource_patch = patch.object(resource_platform, 'adapter_for', return_value=resource_observer)
resource_patch.start()
'''
deferred_case = sys.argv[1:] == ['--deferred']
assert not sys.argv[1:] or deferred_case, 'unexpected fixture arguments'


def audit(event, args):
    if event == 'import' and args[0].split('.')[0] in ('torch', 'transformers', 'laya'):
        raise AssertionError('model import forbidden in synthetic hook fixture')
    if event == 'subprocess.Popen':
        argv = args[1]
        if '-I' in argv:
            raise AssertionError('isolated service/model child forbidden in hook fixture')
        if any(str(x).endswith(('laya_adapter.py', 'laya_inference_worker.py')) for x in argv):
            raise AssertionError('model execution forbidden in synthetic hook fixture')


sys.addaudithook(audit)
def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def progress(stage,project=None):
    print(canonical({'fixtureStage':stage,'project':project.name if project else None}),file=sys.stderr,flush=True)


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(canonical(value)+'\n'); path.chmod(0o600)


def rows(namespace, table):
    path = namespace/('learning.sqlite3' if table == 'samples' else 'shared-learning.sqlite3')
    if not path.exists(): return []
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        return db.execute('SELECT * FROM '+table).fetchall()


def settle_synthetic_inputs(paths):
    # First access can asynchronously add macOS tracking/hidden flags to new
    # Documents files. Settle fixture inputs before accepting any grant; never
    # mask or replace the production before/after metadata checks.
    if sys.platform=='darwin':
        if not hasattr(os,'chflags'):raise AssertionError('fixture_mac_flags_unavailable')
        for path in paths:
            assert path.is_relative_to(base) and path.stat().st_nlink==1
            try:os.chflags(path,path.stat().st_flags|stat.UF_HIDDEN)
            except OSError as error:raise AssertionError('fixture_mac_flags_refused') from error
    for path in paths:
        path.read_bytes()
    if sys.platform!='darwin':return
    deadline=time.monotonic()+5;stable=time.monotonic()
    previous=[(p.stat().st_ctime_ns,p.stat().st_flags) for p in paths]
    while time.monotonic()<deadline:
        time.sleep(.1)
        current=[(p.stat().st_ctime_ns,p.stat().st_flags) for p in paths]
        if current!=previous:previous=current;stable=time.monotonic()
        elif time.monotonic()-stable>=1:return
    raise AssertionError('synthetic input metadata did not settle')


# Product scope refuses system temp roots. On macOS also avoid synchronized
# Documents: FileProvider opens these newly created lock/metadata files.
# Only this disposable fixture moves; the source checkout stays unchanged.
fixture_parent=Path(pwd.getpwuid(os.getuid()).pw_dir) if sys.platform=='darwin' else REPO
with tempfile.TemporaryDirectory(prefix='qmd-hook-validation-', dir=fixture_parent) as raw:
    base = Path(raw).resolve(); home = base/'home'; home.mkdir(mode=0o700)
    bin_dir = home/'bin'; bin_dir.mkdir(mode=0o700)
    # Scope grants bind an owner-controlled runtime, not CI's ambient Python.
    # No model command may execute in this fixture; keep that identity synthetic.
    model_python = bin_dir/'synthetic-model-python'
    model_python.write_text('#!/bin/sh\nexit 97\n'); model_python.chmod(0o700)
    temporary=base/'tmp';temporary.mkdir(mode=0o700)
    environment = {k:v for k,v in os.environ.items() if not k.startswith(('QMD_', 'GEMINI_', 'CLAUDE_', 'CODEX_'))}
    environment.update(HOME=str(home), CODEX_HOME=str(home/'.codex'),
        TMPDIR=str(temporary),QMD_LOCK_BASE=str(base/'writer-locks'),
        PATH=str(bin_dir)+os.pathsep+os.environ.get('PATH', os.defpath),
        PYTHONDONTWRITEBYTECODE='1', QMD_RECALL_LOG='',
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1')
    os.environ.clear(); os.environ.update(environment)
    sys.path.insert(0,str(REPO/'test/helpers'))
    from uid_home import isolate
    codex = bin_dir/'codex'
    codex.write_text('#!'+sys.executable+'\n'+'''import json,sys
from pathlib import Path
home=Path.home()
if sys.argv[1:3]==['plugin','list']:
 print((home/'inventory.json').read_text())
elif sys.argv[1]=='app-server':
 for line in sys.stdin:
  item=json.loads(line); ident=item.get('id'); method=item.get('method')
  if ident is None: continue
  result={} if method=='initialize' else {'account':{'type':'chatgpt'}} if method=='account/read' else {'rateLimits':{'secondary':{'windowDurationMins':10080,'usedPercent':10}}}
  print(json.dumps({'id':ident,'result':result}),flush=True)
else:
 raise SystemExit('unexpected external model command')
'''); codex.chmod(0o700)
    # Project-effective installation checks use Claude's official read-only
    # listing too. Keep this fixture at the host boundary; it never starts a
    # Claude model session and cannot invent a selected adapter in product code.
    claude = bin_dir/'claude'
    claude.write_text('#!'+sys.executable+'\n'+'''import sys
from pathlib import Path
if sys.argv[1:]==['plugin','list','--json']:
 print((Path.home()/'claude-inventory.json').read_text())
else:
 raise SystemExit('unexpected Claude model command')
'''); claude.chmod(0o700)
    version = json.loads((REPO/'package.json').read_text())['version']
    installs = {}
    for host in ('claude', 'codex'):
        target = home/('.'+host)/'plugins/cache/qmd-auto-context-marketplace/qmd-auto-context'/version
        target.mkdir(parents=True, mode=0o700)
        for directory in ('core', 'hooks'):
            shutil.copytree(REPO/directory, target/directory,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for relative in ('package.json', '.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
            save(target/relative, json.loads((REPO/relative).read_text()))
        installs[host] = target
    # Execute the copied installed code, exactly as an enrolled host does;
    # a source checkout is correctly rejected as an execution target.
    sys.path.insert(0,str(installs['codex']/'core'))
    isolate(home,observation_bootstrap='\nsys.path.insert(0,'+repr(str(installs['codex']/'core'))+')\n'
        +RESOURCE_OBSERVER+'\nsys.path.pop(0)\n')
    import resource_platform
    resource_observer=resource_platform.adapter_for(sys.platform)
    from context_learning import general_teacher, shared, project_scope
    from context_learning.resource_coordination import Coordinator
    from runtime_work import WakeQueue
    assert Coordinator().root.is_relative_to(home)
    assert WakeQueue().root.is_relative_to(home)
    scheduler_lock=open(Coordinator().root/'runtime-worker.lock','a')
    os.chmod(scheduler_lock.name,0o600);fcntl.flock(scheduler_lock,fcntl.LOCK_EX)
    from context_learning.contracts import digest
    from context_learning.laya_adapter import MODEL_FILES
    import project_learning_setup as setup
    import qmd_shared_store
    import runtime_update
    save(home/'.claude/plugins/known_marketplaces.json', {'qmd-auto-context-marketplace':{
        'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}}})
    save(home/'.claude/plugins/installed_plugins.json', {'plugins':{
        'qmd-auto-context@qmd-auto-context-marketplace':[{'version':version,'installPath':str(installs['claude'])}]}})
    save(home/'claude-inventory.json', [{'id':'qmd-auto-context@qmd-auto-context-marketplace',
        'version':version,'installPath':str(installs['claude']),'enabled':True}])
    save(home/'inventory.json', {'installed':[{'pluginId':'qmd-auto-context@qmd-auto-context-marketplace',
        'name':'qmd-auto-context','marketplaceName':'qmd-auto-context-marketplace',
        'version':version,'installed':True,'enabled':True,
        'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}}]})
    model = base/'synthetic-model'; model.mkdir(mode=0o700)
    for name in MODEL_FILES:
        file = model/name; file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file.write_text('synthetic file only, not a model'); file.chmod(0o600)
    save(model/'rl_agent_config.json', {'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256})
    model_files=[model/name for name in MODEL_FILES]
    initial=[file.read_bytes() for file in model_files]
    copied_files=[p for root in installs.values() for p in root.rglob('*')
                  if p.is_file() and p.suffix!='.pyc']
    settle_synthetic_inputs(model_files+copied_files)
    assert [file.read_bytes() for file in model_files]==initial
    artifact = base/'immutable-base.artifact'; artifact.write_text('synthetic artifact'); artifact.chmod(0o600)
    from context_learning.runtime_setup import default_managed_root
    namespace_parent = default_managed_root(home).parent.parent/'live-selection'
    namespace_parent.mkdir(mode=0o700, parents=True)
    generation = qmd_shared_store.root()/'generations'/('g-'+'1'*32)
    for directory in (generation, generation/'config', generation/'cache'):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    qmd_shared_store.root().chmod(0o700)
    (generation.parent).chmod(0o700)
    index = generation/'index.sqlite'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
        db.execute('CREATE TABLE content(hash TEXT,body TEXT)')
        db.execute('CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT)')
        db.execute('CREATE TABLE store_collections(name TEXT)')
    index.chmod(0o600)
    config_file = generation/'config/index.yml'; config_file.write_text('collections: {}\n'); config_file.chmod(0o600)
    save(generation/'prepared.json', {'schema':qmd_shared_store.SCHEMA,'generation':generation.name,
        'configSha256':digest(config_file.read_text()),
        'indexSchemaSha256':runtime_update.inspect_index(index)['schemaSha256']})
    save(qmd_shared_store.root()/'active.json', {'schema':qmd_shared_store.SCHEMA,'generation':generation.name})
    projects = []; registry = {}; locks = [scheduler_lock]
    for label in ('service-engineering', 'rccar'):
        project = home/'work'/label; wiki = project/'.auto-context/wiki'
        wiki.mkdir(mode=0o700, parents=True); (project/'docs').mkdir(); (project/'.git').mkdir()
        collection = label+'-wiki'; source = project/'docs/cobalt.md'; source.write_text('Synthetic Cobalt garden.\n')
        revision = {'kind':'file','path':'docs/cobalt.md','collection':label+'-docs',
            'sha256':digest(source.read_text()),'size':source.stat().st_size,'mtimeNs':source.stat().st_mtime_ns}
        card = wiki/'cobalt.md'
        card.write_text('---\ntitle: Synthetic Cobalt garden\nstatus: verified\ncreatedBy: qmd-auto-context\nsourceRevisions:\n  - '+json.dumps(revision)+'\n---\nSynthetic Cobalt garden flowers.\n')
        with sqlite3.connect(index) as db:
            db.execute('INSERT INTO documents VALUES (?,?,?,1)', (collection,card.name,digest(card.read_text())))
        cfg = {'indexing':True,'collections':[collection], 'collectionPaths':{collection:'.auto-context/wiki'},
            'collectionRoles':{collection:'wiki'},'recallStrategy':'wikiOnly','topN':3,'minScore':0,
            'events':['userPromptSubmit'],'contextLearning':{'capture':False,'autoCycle':False,'liveSelection':True}}
        save(project/'.auto-context/settings.json',cfg)
        registry[collection] = {'scope':str(project),'path':str(wiki),'indexed':True}
        namespace = namespace_parent/shared._project_key(project); namespace.mkdir(mode=0o700)
        save(namespace/'live-selector.json', {'schema':'qmd-live-selector-v1',
            'trainerArgv':[str(model_python),str(installs['codex']/'core/context_learning/laya_adapter.py'),str(model)],
            'timeoutSeconds':30,'maxRssMib':4096})
        save(namespace/'active-checkpoint.json', {'artifact_path':str(artifact),
            'artifact_sha256':setup.file_sha(artifact),'cycle_id':'synthetic-base'})
        fixture = base/(label+'-hits.json'); save(fixture,{'results':[
            {'file':collection+'/cobalt.md','score':.99,'title':'Synthetic Cobalt','line':1}]})
        projects.append((project,namespace,fixture))
    save(qmd_shared_store.root()/'collections.json',registry)
    # No setup guard, process identity replacement, or selector patch.
    child = '''import sys
attempts=[]
def audit(event,args):
 if event=='import' and args[0].split('.')[0] in ('torch','transformers','laya'):
  attempts.append(event)
  raise AssertionError('model import forbidden')
 if event=='subprocess.Popen' and any(str(x).endswith(('laya_adapter.py','laya_inference_worker.py')) for x in args[1]):
  attempts.append(event)
  raise AssertionError('model subprocess forbidden')
 if event=='subprocess.Popen' and '-I' in args[1]:
  raise AssertionError('isolated service/model subprocess forbidden')
sys.addaudithook(audit)
sys.path.insert(0,sys.argv[1])
import resource_platform
assert resource_platform.adapter_for(sys.platform).level=='normal'
from pathlib import Path
import os,pwd
assert Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()==Path.home().resolve()
from context_learning.resource_coordination import Coordinator
from runtime_work import WakeQueue
assert Coordinator().root.is_relative_to(Path.home())
assert WakeQueue().root.is_relative_to(Path.home())
import recall,hook_main
hook_main.run(recall.main)
assert not attempts,attempts
'''
    def recall(project,fixture,host,prompt):
        env = {**os.environ,'QMD_QUERY_FIXTURE':str(fixture),'QMD_ENGINE':host,
            'QMD_RECALL_LOG':str(base/'recall.jsonl')}
        result = subprocess.run([sys.executable,'-B','-c',child,str(installs[host]/'core')],
            input=canonical({'cwd':str(project),'prompt':prompt,'hook_event_name':'UserPromptSubmit'}),
            text=True,capture_output=True,env=env,cwd=project,timeout=20,check=True)
        assert 'Synthetic Cobalt' in result.stdout, (result.stdout,result.stderr,(base/'recall.jsonl').read_text())
    request_nonces = set()
    def wait_requested(project,namespace):
        """The hook persists a new full-worker request and its durable wake."""
        deadline=time.monotonic()+15; path=namespace/shared.FULL_WORKER_REQUEST
        while time.monotonic()<deadline:
            if path.exists():
                nonce=json.loads(path.read_text())['nonce']
                if nonce not in request_nonces:
                    request_nonces.add(nonce)
                    wakes={(row['kind'],row['target']) for row in WakeQueue().pending()}
                    assert ('learning',str(project.resolve())) in wakes,wakes
                    return
            time.sleep(.05)
        def diagnostic(read):
            try:return read()
            except Exception as error:return 'unavailable:'+type(error).__name__
        raise AssertionError('full-worker request not persisted: '+str(namespace)
            +' shared='+diagnostic(lambda:(namespace/'shared-last-status.json').read_text()[:2048]
                if (namespace/'shared-last-status.json').exists() else 'missing')
            +' captures='+str(diagnostic(lambda:len(rows(namespace,'samples'))))
            +' cases='+str(diagnostic(lambda:len(rows(namespace,'cases')))))
    def recall_until_enqueued(project, namespace, fixture, host, prompt, case_count):
        """Retry only a captured hook whose admission timed out before enqueue.

        Each retry is a new prompt event. It is safe only when the prior event
        has a durable local sample and no shared case; a committed case must
        never be retried because that would create another paid teacher case.
        The real preparation deadline and OS observations remain in force.
        """
        for attempt in range(3):
            before = {row[0] for row in rows(namespace, 'samples')}
            assert len(rows(namespace, 'cases')) == case_count
            recall(project, fixture, host, prompt)
            captured = rows(namespace, 'samples')
            new_ids = {row[0] for row in captured} - before
            assert len(new_ids) == 1, (project, host, 'capture_not_unique', len(new_ids))
            status_path = namespace / 'shared-last-status.json'
            assert status_path.is_file(), (project, host, 'shared_status_missing')
            status = json.loads(status_path.read_text())
            assert status.get('request_id_hash') == digest(next(iter(new_ids))), status
            if status.get('status') == 'queued':
                assert len(rows(namespace, 'cases')) == case_count + 1, status
                wait_requested(project, namespace)
                return
            assert (status.get('status'), status.get('stage'), status.get('reason')) == (
                'unavailable', 'enqueue', 'preparation_budget'), status
            assert len(rows(namespace, 'cases')) == case_count, status
            if attempt < 2:
                time.sleep(.25)
        raise AssertionError('bounded preparation admission retries exhausted: '
            + canonical({'project':project.name, 'host':host, 'status':status}))
    # Finish both setup transactions before enabled captures start workers.
    for project,namespace,fixture in projects:
        recall(project,fixture,'claude','Where do synthetic Cobalt garden flowers grow?')
        assert not rows(namespace,'samples') and not rows(namespace,'cases')
        now = int(time.time()*1000)
        plan = setup.prepare(project,namespace,expires_at_unix_ms=now+3600000,not_before_unix_ms=now-1000)
        try:
            setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan))
        except Exception as error:
            if str(error) in ('learning_compute_busy','learning_deferred_inference'):
                root=Coordinator().root
                diagnostic=subprocess.run(['lsof','-nP','+d',str(root)],text=True,capture_output=True,timeout=2)
                print(canonical({'fixturePid':os.getpid(),'project':project.name,'coordinationRoot':str(root),
                    'holders':diagnostic.stdout}),file=sys.stderr)
            if str(error)=='project_scope_model_changed':
                previous=project_scope._MODEL_CACHE.get((str(namespace),str(model)))
                current=tuple(project_scope._stat_identity(model/name) for name in MODEL_FILES)
                print(canonical({'modelMetadataBefore':previous[1],
                    'modelMetadataAfter':current}),file=sys.stderr)
            raise
        lock = open(namespace/'project-worker.lock','a'); os.chmod(lock.name,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX); locks.append(lock)
        progress('activated',project)
    for project,namespace,fixture in projects:
        recall_until_enqueued(project,namespace,fixture,'claude',
            'Where do synthetic Cobalt garden flowers grow?',0)
        recall_until_enqueued(project,namespace,fixture,'codex',
            'Which synthetic Cobalt garden flowers are documented?',1)
        with sqlite3.connect(namespace/'learning.sqlite3') as db:
            samples = [json.loads(row[0]) for row in db.execute('SELECT body FROM samples')]
        assert {row['host'] for row in samples} == {'claude','codex'}, samples
        assert len(rows(namespace,'cases')) == 2, (namespace, (base/'recall.jsonl').read_text())
        progress('both_hosts_captured_workers_finished',project)
    if deferred_case:
        # Keep the real provider/admission path; change only the OS observation.
        # Fresh fixture HOME/UID roots prevent another test or user lease from
        # deciding either scenario. Detached workers have already exited busy.
        policies={str(namespace):shared._policy(namespace) for _,namespace,_ in projects}
        resource_observer.level = 'warning'
        from context_learning.resource_coordination import decision
        import runtime_admission
        observed = runtime_admission.sample(runtime_admission.POLICY)
        assert observed['memory_pressure']['level']=='warning' and observed['ready'] is False
        assert decision(runtime_admission.POLICY,observed)['ready'] is False
        for project,namespace,_ in projects:
            before = rows(namespace,'cases')
            pointer = (namespace/'active-checkpoint.json').read_bytes()
            settings = (project/'.auto-context/settings.json').read_bytes()
            def forbidden(*args,**kwargs):
                raise AssertionError('deferred teacher invoked')
            result = general_teacher.process_pending(namespace,project,policies[str(namespace)],invoke=forbidden,max_cases=2)
            assert result['status']=='deferred_resource' and result['reason']=='learning_deferred_resource',result
            assert result['outcomes']==[] and result['cli_invocations']==0,result
            assert rows(namespace,'cases')==before and len(before)==2
            assert (namespace/'active-checkpoint.json').read_bytes()==pointer
            assert (project/'.auto-context/settings.json').read_bytes()==settings
            with sqlite3.connect((namespace/'shared-learning.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
                assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_gold_attempts'").fetchone()
        assert resource_observer.calls>0 and not os.environ.get('QMD_SETUP_GUARD_FIXTURE')
        for lock in locks:lock.close()
        print(canonical({'resourceCase':'deferred','resourceObservationsInjected':True,
            'privateCoordination':True,'deferralPreservesCases':True,'noGuardOverrides':True,
            'modelCalls':0,'externalCalls':0}))
        raise SystemExit(0)
    for project,namespace,_ in projects:
        with shared._db(namespace) as db:
            assert {r[0] for r in db.execute('SELECT DISTINCT project_root FROM cases')} == {str(project)}
        pointer = (namespace/'active-checkpoint.json').read_bytes()
        calls = []
        def invoke(sample,bodies,*args,**kwargs):
            kwargs['guard']()
            assert general_teacher.payload(sample,bodies)['question'] == sample['prompt']['text']
            calls.append(sample['request_id'])
            return {'selected_ids':[sample['candidates'][0]['candidate_id']],'usage':{}}
        try:
            policy = shared._policy(namespace)
            result = general_teacher.process_pending(namespace,project,policy,invoke=invoke,max_cases=2)
        except ValueError as error:
            if str(error)=='project_scope_model_changed':
                previous = project_scope._MODEL_CACHE.get((str(namespace),str(model)))
                current = tuple(project_scope._stat_identity(model/name) for name in MODEL_FILES)
                print(canonical({'modelMetadataBefore':previous[1], 'modelMetadataAfter':current}), file=sys.stderr)
            raise
        if result['outcomes']!=['gold','gold']:
            previous = project_scope._MODEL_CACHE.get((str(namespace),str(model)))
            current = tuple(project_scope._stat_identity(model/name) for name in MODEL_FILES)
            print(canonical({'modelMetadataBefore':previous[1], 'modelMetadataAfter':current,
                'calls':len(calls),'result':result}),file=sys.stderr)
        assert result['outcomes']==['gold','gold'], result
        assert len(calls)==4
        with shared._db(namespace) as db:
            assert db.execute("SELECT count(*) FROM project_gold_attempts WHERE stage='committed'").fetchone()==(4,)
        # A fresh connection/worker reentry cannot spend again on committed gold.
        again = general_teacher.process_pending(namespace,project,policy,invoke=invoke,max_cases=2)
        assert again['status']=='drained' and len(calls)==4, again
        assert (namespace/'active-checkpoint.json').read_bytes()==pointer
        assert not (namespace/'experimental-checkpoint.json').exists()
        progress('gold_committed_and_duplicate_wake_drained',project)
    # Sensitive query is captured locally but never passed to teacher transport.
    project,namespace,fixture = projects[0]
    recall_until_enqueued(project,namespace,fixture,'codex',
        'Find synthetic Cobalt garden password="never-send-this-secret"',2)
    result = general_teacher.process_pending(namespace,project,shared._policy(namespace),
        invoke=lambda *a,**k:(_ for _ in ()).throw(AssertionError('secret crossed transport')))
    assert result['outcomes']==['excluded_sensitive'], result
    progress('sensitive_case_excluded',project)
    for project,namespace,_ in projects:
        pointer = (namespace/'active-checkpoint.json').read_bytes()
        try:
            answer = shared.run_training_pending(namespace,project)
        except ValueError as error:
            if str(error)=='project_scope_model_changed':
                previous = project_scope._MODEL_CACHE.get((str(namespace),str(model)))
                current = tuple(project_scope._stat_identity(model/name) for name in MODEL_FILES)
                print(canonical({'modelMetadataBefore':previous[1], 'modelMetadataAfter':current}),file=sys.stderr)
            raise
        assert answer['status'] in ('waiting_fresh_train','waiting_train_cohort'), answer
        assert (namespace/'active-checkpoint.json').read_bytes()==pointer
        progress('insufficient_training_preserves_checkpoint',project)
    checkpoint_before_drain = {
        str(namespace): (namespace/'active-checkpoint.json').read_bytes()
        for _,namespace,_ in projects}
    receipts=[json.loads(path.read_text()) for path in (home/'.uid-home-fixture/receipts').glob('*.json')]
    # Hooks never spawn the worker; only the runtime dispatcher may.
    worker_receipts=[row for row in receipts if row['argv0'].endswith('context_learning_shared.py')]
    assert worker_receipts==[],worker_receipts
    for lock in locks: lock.close()
    from runtime_work import drain
    # The dispatcher starts one owned worker per pending request and keeps its
    # wake until that worker settles; teacher work was already committed above.
    # A child may finish while a different, detached dispatcher owns the UID
    # lease. In that case our drain result is only a snapshot, not the final
    # queue outcome. Wait for the durable ACK rather than requiring this caller
    # to be the dispatcher that observes the empty queue.
    deadline=time.monotonic()+120
    drain_results=[]
    queue=WakeQueue()
    while time.monotonic()<deadline:
        drained=drain(queue,max_seconds=min(15,max(1,deadline-time.monotonic())),max_runs=8)
        drain_results.append(drained)
        drain_results=drain_results[-16:]
        if not queue.pending() and all(
            json.loads((namespace/shared.FULL_WORKER_STATE).read_text())['acked']
            for _,namespace,_ in projects):
            break
        if drained['status']=='busy':time.sleep(1)
    else:
        pending=[{key:row[key] for key in ('kind','status','attempts','reason')}
                 for row in queue.pending()]
        states=[json.loads((namespace/shared.FULL_WORKER_STATE).read_text())
                for _,namespace,_ in projects]
        raise AssertionError({'pending':pending,'workerStates':states,
                              'drainResults':drain_results})
    receipts=[json.loads(path.read_text()) for path in (home/'.uid-home-fixture/receipts').glob('*.json')]
    worker_receipts=[row for row in receipts if row['argv0'].endswith('context_learning_shared.py')]
    # Compute admission can defer one child while another project trains.
    # Coalesced wake revisions may each observe the same insufficient cohort;
    # these are read-only training admissions, not another paid teacher cycle.
    assert len(worker_receipts)>=len(projects) and all(row['home']==str(home) for row in worker_receipts),worker_receipts
    for _,namespace,_ in projects:
        assert json.loads((namespace/shared.FULL_WORKER_STATE).read_text())['acked'] is True
        outcomes=[json.loads(line)['status'] for line in (namespace/'worker.log').read_text().splitlines()]
        assert outcomes.count('waiting_fresh_train')>=1 and set(outcomes)<={
            'deferred_resource','waiting_fresh_train'},outcomes
        assert (namespace/'active-checkpoint.json').read_bytes()==checkpoint_before_drain[str(namespace)]
        with shared._db(namespace) as db:
            assert db.execute("SELECT count(*) FROM project_gold_attempts WHERE stage='committed'").fetchone()==(4,)
    progress('private_retry_queue_drained')
    assert not any(name in sys.modules for name in ('torch','transformers','laya'))
    assert not os.environ.get('QMD_SETUP_GUARD_FIXTURE')
    assert resource_observer.calls>0
    print(canonical({'disabledNoHistory':True,'bothHostsShareProjectNamespace':True,
        'projectsRemainIsolated':True,'durableGold':True,'secretExcludedBeforeTransport':True,
        'insufficientDataDoesNotPromote':True,'noModelImports':True,'noGuardOverrides':True,
        'resourceCase':'normal','resourceObservationsInjected':True,'privateCoordination':True,
        'modelCalls':0,'externalCalls':0}))
