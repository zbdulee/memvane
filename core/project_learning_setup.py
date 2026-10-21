"""Explicit reviewed project enrollment; config-only recovery never rolls back data.

This maintenance entry point does not call teacher, trainer, QMD or host sessions.
Prepare is read-only. Activate binds an exact plan and keeps the shared worker
closed until the final durable journal phase. Recover restores only changed
configuration after compare-and-swap checks, preserving new captures/candidates.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
import tempfile
import uuid
import math

from context_learning import project_scope, learning_profile
from context_learning.dataset import read_private_json
from context_learning.inference_identity import code_identity, file_sha
from context_learning.laya_adapter import EDIT_INPUT_VIEW, model_identity
from context_learning.local_cycle import _atomic_json
from context_learning.shared import _project_key
from context_learning.store import canonical
from context_learning.training_cursor import _sync

NAME='project-learning-setup.json'
SCHEMA='qmd-project-learning-setup-v1'
SYNTHETIC_STAGED='synthetic-staged'
SYNTHETIC_RESERVE='reviewed-synthetic-qa-floor'
CALIBRATION='synthetic-memory-calibration.json'
BUDGET='synthetic-run-budget.json'
BACKUPS='project-learning-backups'
CONFIG_NAMES=('shared-policy.json',project_scope.NAME,'project-teacher-consent.json',
              learning_profile.NAME,learning_profile.INSTALL)


def proposal_sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _path(raw, *, directory=False, private=False, missing=False):
    path=Path(raw)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('setup_unsafe_path')
    if not path.exists():
        if missing:return path
        raise ValueError('setup_missing_path')
    info=path.stat()
    if (info.st_uid!=os.getuid() or info.st_mode&(0o077 if private else 0o022)
        or (directory and not stat.S_ISDIR(info.st_mode))
        or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1))):
        raise ValueError('setup_unsafe_path')
    return path.resolve(strict=True)


def _namespace(project,root,*,mode='default'):
    project=_path(project,directory=True)
    root=_path(root,directory=True,private=True)
    if mode==SYNTHETIC_STAGED:
        fixture=project.parent
        def fixture_record(name):
            path=_path(fixture/name)
            if path.stat().st_size>1024*1024:raise ValueError('setup_synthetic_fixture_invalid')
            before=file_sha(path)
            try:value=json.loads(path.read_text(encoding='utf8'))
            except (UnicodeError,ValueError) as exc:raise ValueError('setup_synthetic_fixture_invalid') from exc
            if file_sha(path)!=before or not isinstance(value,dict):
                raise ValueError('setup_synthetic_fixture_invalid')
            return value
        manifest=fixture_record('MANIFEST.json')
        marker=fixture_record('.qa-fixture-owner')
        if (manifest.get('schema')!='qmd-release-qa-fixture-v1'
                or manifest.get('root')!=str(fixture)
                or manifest.get('paths',{}).get('project')!=str(project)
                or marker.get('fixtureId')!=manifest.get('fixtureId')
                or marker.get('uid')!=os.getuid()
                or root!=fixture/'learning-private'):
            raise ValueError('setup_synthetic_fixture_invalid')
        return project,root
    if mode!='default':raise ValueError('setup_mode_invalid')
    from context_learning.runtime_setup import default_managed_root
    expected=default_managed_root().parent.parent/'live-selection'/_project_key(project)
    if root!=expected or root==project or project in root.parents:
        raise ValueError('setup_namespace_invalid')
    return project,root


def _synthetic_marker(root,policy):
    """Any trace of the synthetic QA policy or its staged profile.

    Without the setup journal these traces cannot be proven ordinary, so the
    caller must fail closed instead of silently dropping the synthetic limits.
    """
    memory=policy.get('adaptiveMemory') if isinstance(policy,dict) else None
    if isinstance(memory,dict) and memory.get('reserveSource')==SYNTHETIC_RESERVE:return True
    if isinstance(policy,dict) and policy.get('maxGoldCalls')==0:return True
    path=Path(root)/learning_profile.NAME
    if not path.exists() and not path.is_symlink():return False
    try:row=read_private_json(_path(path,private=True))
    except (OSError,ValueError,TypeError):return True
    proposal=row.get('proposal') if isinstance(row,dict) else None
    return not isinstance(proposal,dict) or proposal.get('choice')=='local-staged'


def synthetic_local_only(root,project,policy):
    """Fail closed for an activated, exact local QA setup; no teacher authority."""
    root=Path(root)
    path=root/NAME
    marked=_synthetic_marker(root,policy)
    if not path.exists() and not path.is_symlink():
        if marked:raise ValueError('setup_synthetic_journal_required')
        return False
    try:row=read_private_json(_path(path,private=True))
    except Exception as exc:
        if marked:raise ValueError('setup_synthetic_journal_required') from exc
        raise
    plan=row.get('plan') if isinstance(row,dict) else None
    if not isinstance(plan,dict) or plan.get('mode')!=SYNTHETIC_STAGED:
        if marked:raise ValueError('setup_synthetic_scope_changed')
        return False
    from context_learning.learning_profile import require as require_profile
    reviewed={k:v for k,v in policy.items() if not k.startswith('_')}
    if (row.get('schema')!=SCHEMA or row.get('phase')!='activated'
            or row.get('acceptedSha256')!=proposal_sha256(plan)
            or plan.get('projectRoot')!=str(Path(project).resolve(strict=True))
            or plan.get('namespaceRoot')!=str(root.resolve(strict=True))
            or plan.get('policy')!=reviewed
            or reviewed.get('maxGoldCalls')!=0
            or plan.get('teacherConsent') is not None
            or (root/'project-teacher-consent.json').exists()
            or (root/'project-teacher-consent.json').is_symlink()):
        raise ValueError('setup_synthetic_scope_changed')
    selected=require_profile(root,policy,'local_train')
    if (selected.get('choice')!='local-staged' or selected.get('autoApply') is not False
            or selected.get('localTrain') is not True
            or selected.get('settings',{}).get('contextLearning',{}).get('liveSelection') is not False):
        raise ValueError('setup_synthetic_scope_changed')
    _measured_memory(policy['adaptiveMemory'],plan['memoryEvidence'],root=root,project=Path(project),
        model_identity_sha=plan['modelIdentitySha256'],runtime_sha=file_sha(Path(policy['trainerArgv'][0]).resolve(strict=True)),
        adapter_sha=file_sha(_path(policy['trainerArgv'][1])))
    return True


def synthetic_budget_status(root,project,policy):
    if not synthetic_local_only(root,project,policy):return {'status':'disabled'}
    root=Path(root);path=root/BUDGET
    if not path.exists() and not path.is_symlink():return {'status':'synthetic_training_start_required'}
    row=read_private_json(_path(path,private=True))
    setup=read_private_json(_path(root/NAME,private=True))
    if (not isinstance(row,dict) or set(row)!={'schema','acceptedSha256','startedAtUnixMs','deadlineUnixMs'}
            or row['schema']!='qmd-synthetic-run-budget-v1'
            or row['acceptedSha256']!=setup['acceptedSha256']
            or type(row['startedAtUnixMs'])is not int or type(row['deadlineUnixMs'])is not int
            or row['startedAtUnixMs']!=setup['plan']['memoryEvidence']['startedAtUnixMs']
            or row['deadlineUnixMs']!=row['startedAtUnixMs']+60*60*1000):
        raise ValueError('setup_synthetic_budget_changed')
    if time.time()*1000>row['deadlineUnixMs']:return {'status':'synthetic_training_time_exhausted'}
    return {'status':'ready'}


def start_synthetic_budget(root,project,policy):
    root=Path(root)
    with _locked(root):
        status=synthetic_budget_status(root,project,policy)
        if status['status']=='synthetic_training_start_required':
            setup=read_private_json(_path(root/NAME,private=True))
            began=setup['plan']['memoryEvidence']['startedAtUnixMs']
            _write(root/BUDGET,{'schema':'qmd-synthetic-run-budget-v1',
                'acceptedSha256':setup['acceptedSha256'],'startedAtUnixMs':began,
                'deadlineUnixMs':began+60*60*1000})
            return synthetic_budget_status(root,project,policy)
        return status


def _inventory(root):
    result={}
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if relative.parts[0]==BACKUPS or path.name.endswith('.lock') or path.name==NAME:
            continue
        if path.is_symlink():raise ValueError('setup_unsafe_path')
        if path.is_dir():
            _path(path,directory=True,private=True);continue
        _path(path,private=True)
        if path.name.endswith(('.sqlite3','-wal','-shm','-journal')):continue
        result[str(relative)]=file_sha(path)
    return result


def _desired_settings(raw,root,*,mode='default'):
    settings=deepcopy(raw)
    learning=settings.setdefault('contextLearning',{})
    shared=settings.setdefault('sharedLearning',{})
    if not isinstance(learning,dict) or not isinstance(shared,dict):raise ValueError('setup_settings_invalid')
    if shared.get('root') not in (None,str(root)):raise ValueError('setup_namespace_changed')
    if settings.get('indexing') is False:raise ValueError('setup_indexing_disabled')
    learning.update(capture=True,autoCycle=True)
    if mode==SYNTHETIC_STAGED:
        learning['liveSelection']=False
        shared.update(enabled=True,root=str(root),profile=learning_profile.SCHEMA,autoApply=False)
    else:
        learning.setdefault('liveSelection',True)
        if learning['liveSelection'] is not True:raise ValueError('setup_selection_disabled')
        shared.update(enabled=True,root=str(root),profile=learning_profile.SCHEMA,autoApply=True)
    return settings


def _guard_state_root():
    # Match scripts/development_guard.py without importing a command entrypoint.
    import pwd
    home=Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home/'Library/Logs/qmd-development-guard' if sys.platform=='darwin' else home/'.local/state/qmd-development-guard'


def _descends_from(parents,pid,ancestor):
    """Walk a {pid: ppid} map; shared by running and settled guard checks."""
    seen=set()
    while pid in parents and pid not in seen:
        if pid==ancestor:return True
        seen.add(pid);pid=parents[pid]
    return False


def _live_parents():
    """Same-UID {pid: ppid} from the OS process table at this instant."""
    try:
        result=subprocess.run(['/bin/ps','-axo','pid=,ppid=,uid='],stdin=subprocess.DEVNULL,
            capture_output=True,text=True,timeout=3,env={**os.environ,'LC_ALL':'C'})
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise ValueError('setup_model_test_guard_required') from exc
    if result.returncode:raise ValueError('setup_model_test_guard_required')
    parents={}
    for line in result.stdout.splitlines():
        try:pid,ppid,uid=map(int,line.split())
        except ValueError:continue
        if uid==os.getuid():parents[pid]=ppid
    return parents


def _guard_calibration(evidence,*,root,project,model_identity_sha,runtime_sha,adapter_sha):
    """Accept only the fixed local receipt from a settled, owned model-test.

    Returns the guard-derived peak: the larger of the trainer's self-reported
    RSS and the guard's sampled tree RSS while the calibrator was owned, so a
    low self-report cannot lower the profile. These are same-UID private files;
    a same-UID forger is out of scope and nothing here is cryptographically
    authenticated.
    """
    from context_learning.dataset import read_private_json
    from pathlib import Path
    if not isinstance(evidence,dict) or evidence!=read_private_json(_path(root/CALIBRATION,private=True)):
        raise ValueError('setup_synthetic_calibration_required')
    keys={'schema','projectRoot','namespaceRoot','modelIdentitySha256','runtimeSha256',
          'adapterSha256','device','observedPeakRssKib','resourceSamples',
          'configuredCpuThreads','calibrationPid','guardRun','guardNonce','guardChildPid',
          'startedAtUnixMs','finishedAtUnixMs'}
    if (set(evidence)!=keys or evidence['schema']!='qmd-synthetic-forward-calibration-v1'
            or evidence['projectRoot']!=str(project) or evidence['namespaceRoot']!=str(root)
            or evidence['modelIdentitySha256']!=model_identity_sha
            or evidence['runtimeSha256']!=runtime_sha or evidence['adapterSha256']!=adapter_sha
            or evidence['device']!='mps' or evidence['configuredCpuThreads']!=1
            or any(type(evidence[k])is not int or evidence[k]<=0 for k in
                   ('observedPeakRssKib','resourceSamples','calibrationPid','guardChildPid',
                    'startedAtUnixMs','finishedAtUnixMs'))
            or not evidence['startedAtUnixMs']<evidence['finishedAtUnixMs']<=evidence['startedAtUnixMs']+420000
            or evidence['resourceSamples']<2 or evidence['observedPeakRssKib']>3072*1024
            or not isinstance(evidence['guardNonce'],str) or len(evidence['guardNonce'])!=48):
        raise ValueError('setup_synthetic_calibration_required')
    run=Path(evidence['guardRun'])
    if (run.parent!=_guard_state_root()/'runs' or run.is_symlink()
            or not run.is_dir() or not run.name or not all(c in '0123456789abcdef' for c in run.name)):
        raise ValueError('setup_synthetic_calibration_required')
    checkpoint=read_private_json(_path(run/'checkpoint.json',private=True))
    if (checkpoint.get('run')!=str(run) or checkpoint.get('kind')!='model-test'
            or checkpoint.get('status')!='passed' or checkpoint.get('settled') is not True
            or checkpoint.get('completed') is not True
            or checkpoint.get('resourceLimits') is not True
            or checkpoint.get('proofNonce')!=evidence['guardNonce']
            or checkpoint.get('pid')!=evidence['guardChildPid']):
        raise ValueError('setup_synthetic_calibration_required')
    samples=_path(run/'samples.jsonl',private=True)
    if samples.stat().st_size>4*1024*1024:raise ValueError('setup_synthetic_calibration_required')
    observed=0;tree_peak=0;first=last=None
    for line in samples.read_text().splitlines():
        row=json.loads(line)
        if not isinstance(row,dict):raise ValueError('setup_synthetic_calibration_required')
        owners={p['pid']:p.get('ppid') for p in row.get('owned',[])
                if isinstance(p,dict) and type(p.get('pid'))is int}
        if not _descends_from(owners,evidence['calibrationPid'],evidence['guardChildPid']):continue
        tree,elapsed=row.get('tree_rss_bytes'),row.get('elapsed_seconds')
        if (type(tree)is not int or tree<0 or type(elapsed) not in (int,float)
                or not math.isfinite(elapsed) or elapsed<0 or (last is not None and elapsed<last)):
            raise ValueError('setup_synthetic_calibration_required')
        observed+=1;tree_peak=max(tree_peak,tree)
        first=elapsed if first is None else first;last=elapsed
    # The calibrator must be owned in two guard samples, within the same
    # 420 s ceiling as its own receipt plus one maximum guard interval.
    if observed<2 or last-first>430:raise ValueError('setup_synthetic_calibration_required')
    return {'peakRssKib':max(evidence['observedPeakRssKib'],math.ceil(tree_peak/1024)),
            'guardSamples':observed,'guardObservedSeconds':last-first}


def calibrate_synthetic(project,root):
    """Measure exact-model forward RSS without consuming a training update."""
    project,root=_namespace(project,root,mode=SYNTHETIC_STAGED)
    run_raw=os.environ.get('QMD_DEVELOPMENT_GUARD_RUN')
    nonce=os.environ.get('QMD_DEVELOPMENT_GUARD_NONCE')
    if not run_raw or not nonce:raise ValueError('setup_model_test_guard_required')
    run=_path(run_raw,directory=True,private=True)
    if run.parent!=_guard_state_root()/'runs':raise ValueError('setup_model_test_guard_required')
    checkpoint=read_private_json(_path(run/'checkpoint.json',private=True))
    if (checkpoint.get('run')!=str(run) or checkpoint.get('kind')!='model-test'
            or checkpoint.get('status')!='running' or checkpoint.get('proofNonce')!=nonce
            or checkpoint.get('resourceLimits') is not True or type(checkpoint.get('pid'))is not int
            or not _descends_from(_live_parents(),os.getpid(),checkpoint['pid'])):
        raise ValueError('setup_model_test_guard_required')
    if (root/CALIBRATION).exists() or (root/CALIBRATION).is_symlink():
        raise ValueError('setup_existing_calibration')
    started=int(time.time()*1000)
    live=read_private_json(_path(root/'live-selector.json',private=True))
    argv=live.get('trainerArgv')
    if not isinstance(argv,list) or len(argv)!=3:
        raise ValueError('setup_runtime_invalid')
    runtime=Path(argv[0]).resolve(strict=True)
    adapter=_path(argv[1]);model=_path(argv[2],directory=True)
    identity=model_identity(model)
    runtime_sha=file_sha(runtime);adapter_sha=file_sha(adapter)
    pointer=read_private_json(_path(root/'active-checkpoint.json',private=True))
    artifact=_path(pointer['artifact_path'])
    if file_sha(artifact)!=pointer.get('artifact_sha256'):
        raise ValueError('setup_checkpoint_changed')
    installs=project_scope._installed_binding(project_scope._installations())
    if str(adapter) not in {str(Path(row['root'])/'core/context_learning/laya_adapter.py') for row in installs}:
        raise ValueError('setup_adapter_invalid')
    from context_learning.runtime_setup import probe_runtime
    probe=probe_runtime(argv[0])
    if probe.get('status')!='metadata_compatible' or probe.get('mps_available') is not True:
        raise ValueError('setup_synthetic_mps_profile_required')
    from context_learning.local_cycle import LocalTrainer
    from context_learning.shared import resource_report
    calibration_policy={'maxRssMib':3072,'minAvailableMib':768,'maxLoadPerCpu':.75}
    trainer=LocalTrainer(argv,max_seconds=300,max_rss_mib=3072,require_monitor=True,
        pressure_probe=lambda **options:resource_report(calibration_policy,detailed=True,**options))
    sample={'request_id':'synthetic-calibration','input_sha256':'0'*64,
        'prompt':'Which synthetic town has the blue bridge?',
        'candidates':[{'id':'blue','input_kind':'full_compact_wiki_body',
                       'input_text':'Synthetic town Arin has a blue bridge.'},
                      {'id':'red','input_kind':'full_compact_wiki_body',
                       'input_text':'Synthetic town Bori has a red bridge.'}]}
    with tempfile.TemporaryDirectory(prefix='synthetic-calibration-',dir=root) as temp:
        scratch=Path(temp);os.chmod(scratch,0o700)
        request={'schema_version':1,'model_input_view':EDIT_INPUT_VIEW,
            'artifact_path':str(artifact),'cases':[sample],
            'training_limits':{'microbatchSize':1,'effectiveBatchSize':2,'device':'mps',
                'modelIdentitySha256':identity,'reserveFraction':.35}}
        result,resources=trainer.call('predict',request,scratch/'result.json',cwd=scratch)
    finished=max(started+1,int(time.time()*1000))
    if (not isinstance(result.get('predictions'),list) or len(result['predictions'])!=1
            or type(resources.get('observed_peak_rss_kib'))is not int
            or resources['observed_peak_rss_kib']<=0 or resources['observed_peak_rss_kib']>3072*1024
            or type(resources.get('resource_samples'))is not int or resources['resource_samples']<2
            or resources.get('configured_cpu_threads')!=1):
        raise ValueError('setup_synthetic_calibration_incomplete')
    if (model_identity(model)!=identity or file_sha(runtime)!=runtime_sha
            or file_sha(adapter)!=adapter_sha or file_sha(artifact)!=pointer['artifact_sha256']):
        raise ValueError('setup_synthetic_calibration_inputs_changed')
    evidence={'schema':'qmd-synthetic-forward-calibration-v1','projectRoot':str(project),
        'namespaceRoot':str(root),'modelIdentitySha256':identity,
        'runtimeSha256':runtime_sha,'adapterSha256':adapter_sha,
        'device':'mps','observedPeakRssKib':resources['observed_peak_rss_kib'],
        'resourceSamples':resources['resource_samples'],'configuredCpuThreads':1,
        'calibrationPid':os.getpid(),'guardRun':str(run),'guardNonce':nonce,
        'guardChildPid':checkpoint['pid'],'startedAtUnixMs':started,'finishedAtUnixMs':finished}
    _write(root/CALIBRATION,evidence)
    return {'status':'calibration_pending_guard_settlement','receipt':str(root/CALIBRATION),
            'modelIdentitySha256':identity,'observedPeakRssKib':resources['observed_peak_rss_kib'],
            'resourceSamples':resources['resource_samples']}


def _measured_memory(profile,evidence,*,root,project,model_identity_sha,runtime_sha,adapter_sha):
    """Use a measured forward baseline with a fixed conservative train margin.

    This is an estimate of training memory, never a measured training peak.
    The live trainer still enforces its sampled RSS and MPS working-set gates.
    """
    guard=_guard_calibration(evidence,root=root,project=project,model_identity_sha=model_identity_sha,
                             runtime_sha=runtime_sha,adapter_sha=adapter_sha)
    peak=math.ceil(guard['peakRssKib']/1024)
    if (profile['fixedMib']!=max(math.ceil(peak*1.5),peak+512)
            or profile['perPairMib']!=128
            or profile['fixedMib']+profile['perPairMib']>3072
            or profile.get('estimateSource')!='reviewed-forward-envelope:'+proposal_sha256(evidence)):
        raise ValueError('setup_synthetic_measured_profile_required')
    return evidence


def prepare(project,root,*,expires_at_unix_ms,not_before_unix_ms,cohort_id='general-v1',
            mode='default',memory_profile=None,memory_evidence=None):
    project,root=_namespace(project,root,mode=mode)
    if (type(expires_at_unix_ms)is not int or type(not_before_unix_ms)is not int
        or not 0<not_before_unix_ms<expires_at_unix_ms<=not_before_unix_ms+project_scope.MAX_LIFETIME_MS
        or not time.time()*1000<expires_at_unix_ms<=time.time()*1000+project_scope.MAX_LIFETIME_MS):
        raise ValueError('setup_expiry_invalid')
    if not isinstance(cohort_id,str) or not project_scope.re.fullmatch('[a-z0-9][a-z0-9-]{2,63}',cohort_id):
        raise ValueError('setup_cohort_invalid')
    settings_path=_path(project/'.auto-context/settings.json')
    raw=learning_profile._settings(project)
    project_scope._settings_scope(project)
    inventory=_inventory(root)
    if any(name in inventory for name in CONFIG_NAMES):raise ValueError('setup_existing_learning_config')
    journal=root/NAME
    if journal.exists():
        previous=read_private_json(_path(journal,private=True))
        if previous.get('phase')!='recovered':raise ValueError('setup_existing_transaction')
    live=read_private_json(_path(root/'live-selector.json',private=True))
    argv=live.get('trainerArgv')
    if not isinstance(argv,list) or len(argv)!=3 or any(not isinstance(p,str) or not Path(p).is_absolute() for p in argv):
        raise ValueError('setup_runtime_invalid')
    # A venv executable may be a symlink. Bind its exact link routing and bytes;
    # policy/config/model paths themselves must never follow links. Match scoped
    # activation's owner/single-link requirement before any setup writes.
    runtime_path=Path(argv[0]);resolved=runtime_path.resolve(strict=True)
    info=resolved.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
        or info.st_nlink!=1 or info.st_mode&0o022):
        raise ValueError('setup_runtime_invalid')
    adapter=_path(argv[1]);model=_path(argv[2],directory=True)
    from context_learning.laya_adapter import MODEL_FILES
    for name in MODEL_FILES:_path(model/name)
    installs=project_scope._installed_binding(project_scope._installations())
    if str(adapter) not in {str(Path(row['root'])/'core/context_learning/laya_adapter.py') for row in installs}:
        raise ValueError('setup_adapter_invalid')
    pointer=read_private_json(_path(root/'active-checkpoint.json',private=True))
    artifact=_path(pointer['artifact_path'])
    if file_sha(artifact)!=pointer.get('artifact_sha256'):raise ValueError('setup_checkpoint_changed')
    policy={'schema':'qmd-shared-learning-v1','enabled':True,'goldModel':'gpt-6-luna',
            'maxGoldCalls':20,'batchMin':5,'batchMax':128,'targetExact':.95,'trainerArgv':argv,
            'baseArtifact':str(artifact),'maxRssMib':4096,'minAvailableMib':1024,'maxLoadPerCpu':.8,
            'safetySeconds':120,'experimental':True,'modelInputView':EDIT_INPUT_VIEW}
    teacher={'schema':'qmd-project-teacher-consent-v1','projectRoot':str(project),'recipient':'openai',
             'transport':'codex-cli','purpose':'selection-gold','fields':['query','candidate_body','candidate_id','revision_sha256'],
             'model':'gpt-6-luna','maxGoldCalls':20,'maxInputBytes':65536,'maxOutputBytes':65536,
             'timeoutSeconds':60,'maxRssMib':4096,'enabled':True}
    consent={'schema':'qmd-shared-consent-v1','projectRoot':str(project),'sharedRoot':str(root),
             'capture':True,'localTraining':True,'externalTeacher':False}
    if mode==SYNTHETIC_STAGED:
        from context_learning.memory_policy import validate as validate_memory
        identity=model_identity(model)
        try:profile=validate_memory(memory_profile)
        except (TypeError,ValueError,KeyError) as exc:
            raise ValueError('setup_synthetic_memory_profile_required') from exc
        if (profile.get('device')!='mps' or profile.get('modelIdentitySha256')!=identity
                or profile.get('reserveFraction')!=.35
                or profile.get('systemReserveMib')!=768
                or profile.get('reserveSource')!=SYNTHETIC_RESERVE
                or profile.get('maxVerifiedMicrobatchSize',1)!=1):
            raise ValueError('setup_synthetic_memory_profile_required')
        _measured_memory(profile,memory_evidence,root=root,project=project,
            model_identity_sha=identity,runtime_sha=file_sha(resolved),adapter_sha=file_sha(adapter))
        # maxGoldCalls=0: no teacher budget even if a consent file later appears.
        policy.update(maxRssMib=3072,minAvailableMib=768,maxLoadPerCpu=.75,maxGoldCalls=0,
                      batchMax=100,adaptiveMemory=profile,targetHeadroomFraction=.35)
        teacher=None
    elif memory_profile is not None or memory_evidence is not None:
        raise ValueError('setup_mode_invalid')
    return {**({'mode':mode} if mode==SYNTHETIC_STAGED else {}),
            'schema':SCHEMA,'projectRoot':str(project),'namespaceRoot':str(root),
            'expiresAtUnixMs':expires_at_unix_ms,'notBeforeUnixMs':not_before_unix_ms,'cohortId':cohort_id,
            'sourceSettingsSha256':file_sha(settings_path),'namespaceFiles':inventory,
            'codeSha256':code_identity(),'installations':installs,
            'runtime':{'path':str(runtime_path),'resolved':str(resolved),'sha256':file_sha(resolved)},
            'modelIdentitySha256':model_identity(model),'artifactSha256':file_sha(artifact),
            'policy':policy,'teacherConsent':teacher,'localConsent':consent,
            **({'memoryEvidence':memory_evidence} if mode==SYNTHETIC_STAGED else {}),
            'settings':_desired_settings(raw,root,mode=mode)}


@contextmanager
def _locked(root):
    path=root/'project-learning-setup.lock'
    _path(path,private=True,missing=True)
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:raise ValueError('setup_unsafe_lock')
        fcntl.flock(fd,fcntl.LOCK_EX);yield
    finally:os.close(fd)


def _write(path,value):
    _path(path,private=True,missing=True);_path(path.parent,directory=True,private=True)
    _atomic_json(path,value);_sync(path.parent)


def _bytes(path,raw,*,private_parent=True):
    # Backup restoration preserves the original byte representation.
    import tempfile
    _path(path,private=True,missing=True);_path(path.parent,directory=True,private=private_parent)
    fd,name=tempfile.mkstemp(prefix='.restore-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path);_sync(path.parent)
    finally:Path(name).unlink(missing_ok=True)


def _backup(project,root,plan):
    parent=root/BACKUPS
    if not parent.exists():parent.mkdir(mode=0o700)
    _path(parent,directory=True,private=True)
    backup=parent/uuid.uuid4().hex;backup.mkdir(mode=0o700)
    copies=[(project/'.auto-context/settings.json',backup/'settings.json')]
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if relative.parts[0]==BACKUPS or path.name.endswith('.lock') or path.name==NAME:continue
        if path.is_symlink():raise ValueError('setup_unsafe_path')
        if path.is_dir():continue
        _path(path,private=True)
        if path.name.endswith(('-wal','-shm','-journal')):continue
        copies.append((path,backup/'namespace'/relative))
    hashes={}
    for source,destination in copies:
        destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if source.name.endswith('.sqlite3'):
            with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=1) as incoming:
                with sqlite3.connect(destination) as outgoing:incoming.backup(outgoing)
            destination.chmod(0o600)
        else:_bytes(destination,source.read_bytes())
        hashes[str(destination.relative_to(backup))]=file_sha(destination)
    _write(backup/'manifest.json',{'schema':SCHEMA,'acceptedSha256':proposal_sha256(plan),'files':hashes})
    return backup


def activate(plan,*,accepted_sha256,fault=lambda _:None):
    if accepted_sha256!=proposal_sha256(plan):raise ValueError('setup_acceptance_required')
    mode=plan.get('mode','default')
    project,root=_namespace(plan['projectRoot'],plan['namespaceRoot'],mode=mode)
    with _locked(root):
        current=prepare(project,root,expires_at_unix_ms=plan['expiresAtUnixMs'],
                        not_before_unix_ms=plan['notBeforeUnixMs'],cohort_id=plan['cohortId'],
                        mode=mode,memory_profile=plan['policy'].get('adaptiveMemory'),
                        memory_evidence=plan.get('memoryEvidence'))
        if current!=plan:raise ValueError('setup_reviewed_inputs_changed')
        backup=_backup(project,root,plan)
        paths=[name for name in CONFIG_NAMES if name!='project-teacher-consent.json' or mode!='synthetic-staged']+['consents/'+_project_key(project)+'.json']
        journal={'schema':SCHEMA,'phase':'prepared','acceptedSha256':accepted_sha256,
                 'plan':plan,'backupRoot':str(backup),'written':{},'configurationPaths':paths,
                 'sourceSettingsSha256':plan['sourceSettingsSha256']}
        _write(root/NAME,journal);fault('prepared')
        journal['phase']='installing';_write(root/NAME,journal)
        def publish(relative,value):
            target=root/relative
            if not target.parent.exists():target.parent.mkdir(mode=0o700)
            # Journal the intended hash before publishing, so interruption at
            # any write boundary still leaves a safe configuration CAS.
            journal['written'][relative]=hashlib.sha256((canonical(value)+'\n').encode()).hexdigest()
            _write(root/NAME,journal);_write(target,value)
        publish('shared-policy.json',plan['policy'])
        if mode!='synthetic-staged':
            publish('project-teacher-consent.json',{'acceptedSha256':proposal_sha256(plan['teacherConsent']),'proposal':plan['teacherConsent']})
        publish('consents/'+_project_key(project)+'.json',plan['localConsent'])
        scope=project_scope.proposal(project,root,plan['policy'],expires_at_unix_ms=plan['expiresAtUnixMs'],
                                     not_before_unix_ms=plan['notBeforeUnixMs'],cohort_id=plan['cohortId'])
        scope_envelope={'acceptedSha256':project_scope.proposal_sha256(scope),'proposal':scope}
        journal['written'][project_scope.NAME]=hashlib.sha256((canonical(scope_envelope)+'\n').encode()).hexdigest()
        _write(root/NAME,journal)
        project_scope.activate(project,root,plan['policy'],accepted_sha256=scope_envelope['acceptedSha256'],
                               expires_at_unix_ms=plan['expiresAtUnixMs'],not_before_unix_ms=plan['notBeforeUnixMs'],cohort_id=plan['cohortId'])
        fault('scope_written')
        kwargs={'choice':'local-staged' if mode==SYNTHETIC_STAGED else 'local-auto-apply',
                'mode':'preserve','expires_at_unix_ms':plan['expiresAtUnixMs'],'enable_learning':True}
        if mode==SYNTHETIC_STAGED:kwargs['staged_no_selection']=True
        if file_sha(project/'.auto-context/settings.json')!=plan['sourceSettingsSha256']:raise ValueError('setup_settings_changed')
        profile=learning_profile.proposal(project,root,**kwargs)
        if profile['settings']!=plan['settings']:raise ValueError('setup_settings_changed')
        # Pre-record both possible install journal representations for recovery.
        journal['profileProposal']=profile
        journal['written'][learning_profile.NAME]=hashlib.sha256((canonical({'acceptedSha256':learning_profile.digest(profile),'proposal':profile})+'\n').encode()).hexdigest()
        journal['targetSettingsSha256']=hashlib.sha256((canonical(profile['settings'])+'\n').encode()).hexdigest()
        _write(root/NAME,journal)
        learning_profile.activate(project,root,accepted_sha256=learning_profile.digest(profile),fault=fault,**kwargs)
        journal['written'][learning_profile.INSTALL]=file_sha(root/learning_profile.INSTALL)
        journal['phase']='activated';_write(root/NAME,journal)
        return {'status':'activated','acceptedSha256':accepted_sha256,'backupRoot':str(backup),
                'projectRoot':str(project),'namespaceRoot':str(root),'capture':True,'autoCycle':True,
                'teacherModel':None if mode==SYNTHETIC_STAGED else 'gpt-6-luna',
                'maxGoldCalls':0 if mode==SYNTHETIC_STAGED else 20,
                'promotion':'disabled' if mode==SYNTHETIC_STAGED else 'quality_gated'}


def recover(project,root):
    root=Path(root)
    previous=read_private_json(_path(root/NAME,private=True))
    project,root=_namespace(project,root,mode=previous.get('plan',{}).get('mode','default'))
    import install_update
    # Match activation's lock order: namespace first, then the shared project
    # writer lock. Keep both through verification and every restoration write.
    with _locked(root), install_update._locked(project):
        journal=read_private_json(_path(root/NAME,private=True))
        if journal.get('schema')!=SCHEMA or journal.get('plan',{}).get('projectRoot')!=str(project) or journal['plan'].get('namespaceRoot')!=str(root):
            raise ValueError('setup_recovery_invalid')
        if journal['phase']=='recovered':return {'status':'already_recovered'}
        backup=_path(journal['backupRoot'],directory=True,private=True)
        if backup.parent!=root/BACKUPS:raise ValueError('setup_recovery_invalid')
        manifest=read_private_json(_path(backup/'manifest.json',private=True))
        if manifest.get('acceptedSha256')!=journal['acceptedSha256']:raise ValueError('setup_backup_changed')
        for relative,expected in manifest['files'].items():
            if '..' in Path(relative).parts or Path(relative).is_absolute():raise ValueError('setup_backup_changed')
            if file_sha(_path(backup/relative,private=True))!=expected:raise ValueError('setup_backup_changed')
        settings=_path(project/'.auto-context/settings.json')
        if file_sha(settings) not in {journal['sourceSettingsSha256'],journal.get('targetSettingsSha256')}:
            raise ValueError('setup_recovery_settings_changed')
        # Verify all configuration before changing any of it.
        allowed=(set(CONFIG_NAMES)-({'project-teacher-consent.json'} if journal['plan'].get('mode')==SYNTHETIC_STAGED else set()))|{'consents/'+_project_key(project)+'.json'}
        if set(journal['configurationPaths'])!=allowed or not set(journal['written'])<=allowed:
            raise ValueError('setup_recovery_invalid')
        affected=set(journal['written'])
        if journal.get('profileProposal') is not None:affected.add(learning_profile.INSTALL)
        for relative,expected in journal['written'].items():
            path=root/relative
            if path.exists():
                if file_sha(_path(path,private=True))!=expected:raise ValueError('setup_recovery_configuration_changed')
        install=root/learning_profile.INSTALL
        if install.exists() and learning_profile.INSTALL not in journal['written']:
            row=read_private_json(_path(install,private=True));profile=journal.get('profileProposal')
            if row.get('proposal')!=profile or row.get('acceptedSha256')!=learning_profile.digest(profile) or row.get('phase') not in ('intent','settings_written','activated'):
                raise ValueError('setup_recovery_configuration_changed')
        journal['phase']='recovering';_write(root/NAME,journal)
        _bytes(settings,(backup/'settings.json').read_bytes(),private_parent=False)
        for relative in sorted(affected):
            target=root/relative;original=backup/'namespace'/relative
            if original.exists():_bytes(target,original.read_bytes())
            elif target.exists():_path(target,private=True).unlink();_sync(target.parent)
        journal['phase']='recovered';_write(root/NAME,journal)
        return {'status':'recovered','backupRoot':str(backup),'dataPreserved':True,'checkpointChanged':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='operation',required=True)
    p=sub.add_parser('prepare');p.add_argument('project');p.add_argument('root');p.add_argument('--expires-at-unix-ms',type=int,required=True);p.add_argument('--not-before-unix-ms',type=int,required=True)
    p.add_argument('--mode',choices=('default',SYNTHETIC_STAGED),default='default')
    p.add_argument('--memory-profile-json')
    p.add_argument('--memory-evidence-json')
    p=sub.add_parser('calibrate-synthetic');p.add_argument('project');p.add_argument('root')
    p=sub.add_parser('activate');p.add_argument('plan');p.add_argument('--accepted-sha256',required=True)
    p=sub.add_parser('recover');p.add_argument('project');p.add_argument('root')
    args=parser.parse_args()
    if args.operation=='prepare':value=prepare(args.project,args.root,expires_at_unix_ms=args.expires_at_unix_ms,not_before_unix_ms=args.not_before_unix_ms,
        mode=args.mode,memory_profile=read_private_json(_path(args.memory_profile_json,private=True)) if args.memory_profile_json else None,
        memory_evidence=read_private_json(_path(args.memory_evidence_json,private=True)) if args.memory_evidence_json else None)
    elif args.operation=='calibrate-synthetic':value=calibrate_synthetic(args.project,args.root)
    elif args.operation=='activate':value=activate(read_private_json(_path(args.plan)),accepted_sha256=args.accepted_sha256)
    else:value=recover(args.project,args.root)
    print(canonical(value))


if __name__=='__main__':main()
