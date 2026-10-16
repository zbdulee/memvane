"""Explicit short synthetic experiment, never an automatic/installed policy.

Ordinary warning remains closed. This permission binds exact work and expires;
it preserves the raw warning and the original swap baseline. Limits below are
experimental bounds, not calibrated safety guarantees or hardware minima.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import pwd
import stat
import sys
import threading
import time
import uuid
from resource_platform import normal_pressure,number

SCHEMA='qmd-one-shot-calibration-v1'
_states={}
_gate=threading.Lock()

def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def _base(policy):return {k:v for k,v in policy.items() if k!='calibration'}

def _request(request):return {k:v for k,v in request.items() if k not in ('learning_control','training_limits')}

def claim_root():
    home=Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home/('Library/Logs/qmd-development-guard' if sys.platform=='darwin' else '.local/state/qmd-development-guard')

def _private(path):
    p=Path(path);info=p.lstat()
    if (not p.is_absolute() or any(q.is_symlink() for q in (p,*p.parents))
            or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
            or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>65536):
        raise ValueError('calibration_permission_invalid')
    return json.loads(p.read_text())

def _output(request,*,new=False):
    artifact=Path(request['artifact_path']);parent=artifact.parent;info=parent.lstat()
    if (any(q.is_symlink() for q in (artifact,*artifact.parents))
            or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077
            or new and artifact.exists()):raise ValueError('calibration_output_invalid')
    if new:
        allowed={'scratch','calibration-envelope.json','preflight.json'}
        if any(p.name not in allowed for p in parent.iterdir()):raise ValueError('calibration_output_not_new')

def capability(policy):
    """A real live guard lease + matching active record, not embedded JSON alone."""
    value=validate(policy['calibration'],policy)
    path=os.environ.get('QMD_CALIBRATION_ENVELOPE')
    if not path or _sha(_private(path))!=_sha(value):raise ValueError('calibration_guard_missing')
    root=claim_root();claim=_private(root/'calibrations'/(value['runId']+'.json'))
    active=_private(root/'active.json')
    if (claim.get('envelopePath')!=str(Path(path).resolve()) or claim.get('envelopeSha256')!=_sha(value)
            or active.get('calibrationRunId')!=value['runId'] or active.get('run')!=claim.get('run')
            or active.get('calibrationNonce')!=claim.get('nonce')
            or active.get('settled') is not False or active.get('status') not in ('preflight','running')):
        raise ValueError('calibration_guard_missing')
    lease=root/'calibrations'/(value['runId']+'.lock')
    info=lease.lstat()
    if (lease.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
            or info.st_mode&0o077 or info.st_nlink!=1):raise ValueError('calibration_guard_missing')
    fd=os.open(lease,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        raise ValueError('calibration_guard_missing')
    finally:os.close(fd)

def _shape(request):
    cases=request.get('train')
    if (request.get('schema_version')!=1 or request.get('model_input_view')!='tool-edit-8192-choice-v1'
            or not isinstance(request.get('artifact_path'),str) or not Path(request['artifact_path']).is_absolute()
            or not isinstance(cases,list) or len(cases)!=1):raise ValueError('calibration_scope_invalid')
    case=cases[0]
    expected=[{'id':'cobalt','input_kind':'full_compact_wiki_body','input_text':'The synthetic Cobalt beacon code is COBALT-271.'},
              {'id':'garden','input_kind':'full_compact_wiki_body','input_text':'The synthetic garden marker is GARDEN-909.'}]
    if (case.get('prompt')!='What is the synthetic Cobalt beacon code?'
            or case.get('candidates')!=expected or case.get('selected_ids')!=['cobalt']
            or len(json.dumps(request))>4096):raise ValueError('calibration_scope_invalid')

def _policy(policy):
    base=_base(policy);profile=base.get('adaptiveMemory',{})
    if (base.get('minAvailableMib')!=1024 or base.get('maxRssMib')!=3584 or base.get('maxLoadPerCpu')!=.8
            or profile.get('device')!='mps' or profile.get('fixedMib')!=2897 or profile.get('perPairMib')!=312
            or profile.get('systemReserveMib')!=1024 or profile.get('maxVerifiedMicrobatchSize')!=1
            or 'warmAdditionalMib' in profile):raise ValueError('calibration_policy_invalid')
    from .memory_policy import validate
    validate(profile)
    return base

def _metrics(report):
    pressure=report.get('memory_pressure');status=report.get('query_status')
    if (not isinstance(pressure,dict) or not isinstance(status,dict)
            or status.get('memory_pressure')!='ok' or pressure.get('level') not in ('normal','warning')
            or not number(pressure.get('system_free_percent')) or not 10<=pressure['system_free_percent']<=100
            or any(not number(report.get(k)) for k in ('observed_at_mono','available_mib','total_mib','load_per_cpu','swap_used_mib'))
            or report['total_mib']<=0):raise ValueError('calibration_observation_unavailable')
    return pressure['level']

def create(samples,policy,request,*,command,files,now=None):
    now=time.monotonic() if now is None else now
    base=_policy(policy);_shape(request)
    if not isinstance(samples,list) or len(samples)!=3:raise ValueError('calibration_window_unavailable')
    for row in samples:
        if (_metrics(row)!='warning' or row['available_mib']<4233 or row['load_per_cpu']>.8
                or row['swap_used_mib']!=samples[0]['swap_used_mib']):raise ValueError('calibration_window_unavailable')
    stamps=[r['observed_at_mono'] for r in samples]
    if (stamps[2]-stamps[0]<6 or any(not 0<b-a<=4 for a,b in zip(stamps,stamps[1:]))
            or not 0<=now-stamps[-1]<=2):raise ValueError('calibration_window_unavailable')
    return {'schema':SCHEMA,'runId':uuid.uuid4().hex,'issuedMono':now,'expiresMono':now+30,
        'policySha256':_sha(base),'requestSha256':_sha(request),'artifactPath':request['artifact_path'],
        'modelSha256':base['adaptiveMemory']['modelIdentitySha256'],
        'baselineSwapMib':samples[0]['swap_used_mib'],'baselineObservedMono':stamps[-1],
        'baselinePressure':'warning','command':list(command),'files':dict(files),'policy':base,'request':request}

def validate(value,policy,*,now=None):
    now=time.monotonic() if now is None else now
    base=_policy(policy)
    if (not isinstance(value,dict) or value.get('schema')!=SCHEMA
            or not re.fullmatch('[a-f0-9]{32}',str(value.get('runId','')))
            or not number(value.get('issuedMono')) or not number(value.get('expiresMono'))
            or not value['issuedMono']<=now<value['expiresMono']<=value['issuedMono']+30
            or value.get('policySha256')!=_sha(base) or value.get('policy')!=base
            or value.get('modelSha256')!=base['adaptiveMemory']['modelIdentitySha256']
            or value.get('baselinePressure')!='warning' or not number(value.get('baselineSwapMib'))
            or not number(value.get('baselineObservedMono'))
            or value.get('requestSha256')!=_sha(value.get('request'))
            or value.get('artifactPath')!=value.get('request',{}).get('artifact_path')):
        raise ValueError('calibration_permission_invalid')
    _shape(value['request'])
    return value

def load(path,command):
    path=Path(path)
    info=path.lstat()
    if (not path.is_absolute() or path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>65536):
        raise ValueError('calibration_permission_invalid')
    value=_private(path);validate(value,value.get('policy',{}))
    if value.get('command')!=command or not isinstance(value.get('files'),dict) or len(value['files'])!=3:
        raise ValueError('calibration_command_changed')
    names=list(value['files'])
    test=Path(command[-1])
    expected={str(test),str(test.with_name('one-step.py')),str(test.with_name('observed-current-adapter.py'))}
    if test.name!='one-step.test.mjs' or set(names)!=expected:raise ValueError('calibration_command_changed')
    for name,digest in value['files'].items():
        p=Path(name);meta=p.lstat()
        if (not p.is_absolute() or p.is_symlink() or not stat.S_ISREG(meta.st_mode)
                or meta.st_uid!=os.getuid() or meta.st_mode&0o022 or meta.st_size>65536
                or hashlib.sha256(p.read_bytes()).hexdigest()!=digest):raise ValueError('calibration_command_changed')
    _output(value['request'],new=True)
    return value

def verdict(report,policy,*,now=None):
    now=time.monotonic() if now is None else now
    try:
        value=validate(policy['calibration'],policy,now=now)
        capability(policy)
        level=_metrics(report);stamp=report['observed_at_mono'];swap=report['swap_used_mib']
        if not 0<=now-stamp<=2 or report['available_mib']<1024 or report['load_per_cpu']>.8:
            raise ValueError('calibration_resource_budget')
        if swap-value['baselineSwapMib']>=64:raise ValueError('calibration_swap_growth')
        with _gate:
            previous=_states.get(value['runId'],(value['baselineObservedMono'],value['baselineSwapMib'],'warning',False))
            if previous[3]:raise ValueError('calibration_already_stopped')
            dt=stamp-previous[0]
            if dt<0 or dt==0 and swap!=previous[1]:raise ValueError('calibration_observation_order')
            if dt>0 and max(0,swap-previous[1])/dt>=8:raise ValueError('calibration_swap_rate')
            if previous[2]=='normal' and level=='warning':raise ValueError('calibration_pressure_worsening')
            _states[value['runId']]=(stamp,swap,level,False)
        return {'ready':True,'reason':'explicit_bounded_calibration','runId':value['runId']}
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        value=policy.get('calibration')
        if isinstance(value,dict) and isinstance(value.get('runId'),str):
            with _gate:
                old=_states.get(value['runId'],(0,0,'warning',False))
                _states[value['runId']]=(*old[:3],True)
        return {'ready':False,'reason':str(exc)}

def pressure_ready(report,policy,*,now=None):
    if 'calibration' not in policy:return normal_pressure(report)
    return verdict(report,policy,now=now)['ready']

def validate_request(request,*,now=None):
    policy=request.get('learning_control',{}).get('policy',{})
    if 'calibration' not in policy:return
    value=validate(policy['calibration'],policy,now=now)
    capability(policy)
    clean=_request(request);_shape(clean)
    if value['requestSha256']!=_sha(clean):raise ValueError('calibration_request_changed')
    _output(clean)
    limits=request.get('training_limits')
    if limits is not None and limits.get('microbatchSize')!=1:raise ValueError('calibration_scope_invalid')

def reserve_launch(request):
    policy=request.get('learning_control',{}).get('policy',{})
    if 'calibration' not in policy:return
    validate_request(request)
    value=policy['calibration']
    p=claim_root()/'calibrations'/(value['runId']+'.model-launch.json')
    try:fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:raise ValueError('calibration_model_already_launched') from None
    with os.fdopen(fd,'w') as stream:
        json.dump({'runId':value['runId'],'requestSha256':value['requestSha256']},stream);stream.flush();os.fsync(stream.fileno())

def reset_test_state():
    with _gate:_states.clear()
