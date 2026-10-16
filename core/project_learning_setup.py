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
import time
import uuid

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


def _namespace(project,root):
    project=_path(project,directory=True)
    root=_path(root,directory=True,private=True)
    from context_learning.runtime_setup import default_managed_root
    expected=default_managed_root().parent.parent/'live-selection'/_project_key(project)
    if root!=expected or root==project or project in root.parents:
        raise ValueError('setup_namespace_invalid')
    return project,root


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


def _desired_settings(raw,root):
    settings=deepcopy(raw)
    learning=settings.setdefault('contextLearning',{})
    shared=settings.setdefault('sharedLearning',{})
    if not isinstance(learning,dict) or not isinstance(shared,dict):raise ValueError('setup_settings_invalid')
    if shared.get('root') not in (None,str(root)):raise ValueError('setup_namespace_changed')
    if settings.get('indexing') is False:raise ValueError('setup_indexing_disabled')
    learning.update(capture=True,autoCycle=True)
    learning.setdefault('liveSelection',True)
    if learning['liveSelection'] is not True:raise ValueError('setup_selection_disabled')
    shared.update(enabled=True,root=str(root),profile=learning_profile.SCHEMA,autoApply=True)
    return settings


def prepare(project,root,*,expires_at_unix_ms,not_before_unix_ms,cohort_id='general-v1'):
    project,root=_namespace(project,root)
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
    return {'schema':SCHEMA,'projectRoot':str(project),'namespaceRoot':str(root),
            'expiresAtUnixMs':expires_at_unix_ms,'notBeforeUnixMs':not_before_unix_ms,'cohortId':cohort_id,
            'sourceSettingsSha256':file_sha(settings_path),'namespaceFiles':inventory,
            'codeSha256':code_identity(),'installations':installs,
            'runtime':{'path':str(runtime_path),'resolved':str(resolved),'sha256':file_sha(resolved)},
            'modelIdentitySha256':model_identity(model),'artifactSha256':file_sha(artifact),
            'policy':policy,'teacherConsent':teacher,'localConsent':consent,'settings':_desired_settings(raw,root)}


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
    project,root=_namespace(plan['projectRoot'],plan['namespaceRoot'])
    with _locked(root):
        current=prepare(project,root,expires_at_unix_ms=plan['expiresAtUnixMs'],
                        not_before_unix_ms=plan['notBeforeUnixMs'],cohort_id=plan['cohortId'])
        if current!=plan:raise ValueError('setup_reviewed_inputs_changed')
        backup=_backup(project,root,plan)
        paths=list(CONFIG_NAMES)+['consents/'+_project_key(project)+'.json']
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
        kwargs={'choice':'local-auto-apply','mode':'preserve','expires_at_unix_ms':plan['expiresAtUnixMs'],'enable_learning':True}
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
                'teacherModel':'gpt-6-luna','maxGoldCalls':20,'promotion':'quality_gated'}


def recover(project,root):
    project,root=_namespace(project,root)
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
        allowed=set(CONFIG_NAMES)|{'consents/'+_project_key(project)+'.json'}
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
    p=sub.add_parser('activate');p.add_argument('plan');p.add_argument('--accepted-sha256',required=True)
    p=sub.add_parser('recover');p.add_argument('project');p.add_argument('root')
    args=parser.parse_args()
    if args.operation=='prepare':value=prepare(args.project,args.root,expires_at_unix_ms=args.expires_at_unix_ms,not_before_unix_ms=args.not_before_unix_ms)
    elif args.operation=='activate':value=activate(read_private_json(_path(args.plan)),accepted_sha256=args.accepted_sha256)
    else:value=recover(args.project,args.root)
    print(canonical(value))


if __name__=='__main__':main()
