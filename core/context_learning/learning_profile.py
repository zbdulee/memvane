"""Explicit accepted local preferences/apply scope, never a teacher grant."""
from copy import deepcopy
from pathlib import Path
import fcntl,os,stat,time
from .contracts import digest as _digest
from .store import canonical
from .dataset import read_private_json,_json
from .local_cycle import _atomic_json,_safe_file,_hash
from .training_cursor import _sync
from .resource_coordination import _lock
from . import resident

NAME='learning-profile.json'
INSTALL='learning-profile-install.json'
SCHEMA='qmd-local-learning-profile-v1'
CHOICES=('local-auto-apply','local-staged','off')

def digest(value):return _digest(canonical(value))

def present(root):return any((Path(root)/name).exists() or (Path(root)/name).is_symlink() for name in (NAME,INSTALL))

def _settings(project):
    path=Path(project)/'.auto-context/settings.json'
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('profile_settings_unsafe')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o022 or info.st_size>1024*1024:
        raise ValueError('profile_settings_unsafe')
    value=_json(path.read_bytes())
    if not isinstance(value,dict):raise ValueError('profile_settings_invalid')
    return value

def binding(settings):
    return {key:deepcopy(settings.get(key)) for key in ('contextLearning','sharedLearning','indexing','collectionPaths')}

def recommendation(settings):
    return {'recommended':'local-auto-apply','choices':list(CHOICES),'accepted':False,
            'requires':['prepared local model/runtime and inference identity','separate current local_train/quality_evaluate execution approval','exact project profile acceptance'],
            'capture':'preserve; new default false','existingExplicitFalse':'preserved',
            'teacherPermission':'unchanged','changesApplied':0}

def _preferences(raw,root,choice):
    cfg=deepcopy(raw);learning=cfg.setdefault('contextLearning',{});shared=cfg.setdefault('sharedLearning',{})
    if not isinstance(learning,dict) or not isinstance(shared,dict):raise ValueError('profile_settings_invalid')
    if shared.get('root') not in (None,str(root)):raise ValueError('profile_namespace_changed')
    learning.setdefault('capture',False)
    learning.setdefault('autoCycle',choice!='off')
    learning.setdefault('liveSelection',choice=='local-auto-apply')
    shared.setdefault('enabled',choice!='off');shared['root']=str(root);shared['profile']=SCHEMA
    if choice=='off':
        learning['autoCycle']=False;learning['liveSelection']=False;shared['enabled']=False
    enabled=cfg.get('indexing') is not False and shared['enabled'] is True and learning['autoCycle'] is True and choice!='off'
    apply=(enabled and learning['liveSelection'] is True and choice=='local-auto-apply' and shared.get('autoApply') is not False)
    shared['autoApply']=apply
    return cfg,enabled,apply

def proposal(project,root,*,choice,mode,expires_at_unix_ms,enable_learning=False,
             staged_no_selection=False):
    project=Path(project);root=Path(root)
    if (choice not in CHOICES or mode not in ('new','preserve') or not project.is_absolute()
            or any(p.is_symlink() for p in (project,root,*project.parents,*root.parents))):raise ValueError('profile_scope_invalid')
    project=project.resolve(strict=True);root=resident._private_state(root.resolve(strict=True))
    raw=_settings(project)
    if mode=='new' and any(key in raw for key in ('contextLearning','sharedLearning')):
        raise ValueError('profile_existing_settings_require_preserve')
    if type(expires_at_unix_ms)is not int or not time.time()*1000<expires_at_unix_ms<=time.time()*1000+14*86400000:
        raise ValueError('profile_expiry_invalid')
    from .project_scope import present as general_scope_present
    if type(enable_learning)is not bool or (enable_learning and not general_scope_present(root)):
        raise ValueError('profile_explicit_enable_scope_required')
    if type(staged_no_selection)is not bool or (staged_no_selection and
            (choice!='local-staged' or not enable_learning)):
        raise ValueError('profile_staged_scope_required')
    preferences=deepcopy(raw)
    if enable_learning:
        preferences.setdefault('contextLearning',{}).update(capture=True,autoCycle=True)
        preferences.setdefault('sharedLearning',{})['enabled']=True
    if staged_no_selection:
        preferences.setdefault('contextLearning',{})['liveSelection']=False
    cfg,train,apply=_preferences(preferences,root,choice)
    from .shared import _policy,_safe_root
    import config
    # A namespace cannot be a source collection or another project's store.
    if cfg['sharedLearning']['enabled'] and _safe_root(config.normalize_config(cfg),project)!=root:
        raise ValueError('profile_namespace_invalid')
    policy=_policy(root)
    from .runtime_authorization import require_operation,proposal_sha256
    from .laya_adapter import model_identity
    from .inference_identity import code_identity
    grant=require_operation(root,policy,'quality_evaluate')
    if train:require_operation(root,policy,'local_train')
    if grant.get('projectRoot')!=str(project):raise ValueError('profile_project_changed')
    from .project_scope import SCOPE
    minimum=(max(policy['batchMin'],grant['cohort']['minTrainCases']) if grant.get('scope')==SCOPE
             else max(policy['batchMin'],grant['cohort']['minQualityTrainCases']) if apply else policy['batchMin'])
    if apply and policy['batchMax']<minimum:raise ValueError('profile_quality_batch_budget_required')
    return {'schema':SCHEMA,'projectRoot':str(project),'namespaceRoot':str(root),
            'choice':choice,'mode':mode,'localTrain':train,'autoApply':apply,
            'minFreshTrainCases':minimum,
            'operations':(['local_train'] if train else [])+['quality_evaluate']+(['apply'] if apply else []),
            'runtimeApprovalSha256':proposal_sha256(grant),'policySha256':_hash(root/'shared-policy.json'),
            'livePolicySha256':_hash(root/'live-selector.json'),'trainerArgv':policy['trainerArgv'],
            'modelIdentitySha256':model_identity(policy['trainerArgv'][2]),'view':policy['modelInputView'],
            'codeSha256':code_identity(),'expiresAtUnixMs':expires_at_unix_ms,'maxQualityAgeSeconds':3600,
            'sourceSettingsSha256':digest(raw),'settings':cfg,'settingsBindingSha256':digest(binding(cfg))}

def _check_scope(row,root,policy,operation,*,settings=True):
    from .runtime_authorization import require_operation,proposal_sha256
    from .laya_adapter import model_identity
    from .inference_identity import code_identity
    grant=require_operation(root,policy,'local_train' if operation=='local_train' else 'quality_evaluate')
    if (row['schema']!=SCHEMA or row['namespaceRoot']!=str(Path(root).resolve())
            or row['projectRoot']!=grant.get('projectRoot') or row['runtimeApprovalSha256']!=proposal_sha256(grant)
            or row['trainerArgv']!=policy['trainerArgv'] or row['view']!=policy['modelInputView']
            or row['modelIdentitySha256']!=model_identity(policy['trainerArgv'][2])
            or row['codeSha256']!=code_identity() or row['policySha256']!=_hash(Path(root)/'shared-policy.json')
            or row['livePolicySha256']!=_hash(Path(root)/'live-selector.json')
            or time.time()*1000>=row['expiresAtUnixMs'] or operation not in row['operations']):
        raise ValueError('profile_scope_changed_or_revoked')
    if settings and row['settingsBindingSha256']!=digest(binding(_settings(row['projectRoot']))):
        raise ValueError('profile_settings_changed')
    return row

def require(root,policy,operation):
    envelope=read_private_json(Path(root)/NAME);row=envelope['proposal']
    if envelope!={'acceptedSha256':digest(row),'proposal':row}:raise ValueError('profile_acceptance_invalid')
    return _check_scope(row,root,policy,operation)

def selection_enabled(root,project):
    """Cheap preference binding; inference has its own full identity checks."""
    envelope=read_private_json(Path(root)/NAME);row=envelope['proposal']
    if (envelope!={'acceptedSha256':digest(row),'proposal':row} or row['schema']!=SCHEMA
            or row['namespaceRoot']!=str(root) or row['projectRoot']!=str(project)
            or time.time()*1000>=row['expiresAtUnixMs'] or row['choice']=='off'
            or row['settings'].get('indexing') is False
            or row['settingsBindingSha256']!=digest(binding(_settings(project)))):raise ValueError('profile_settings_changed')
    return row['settings']['contextLearning']['liveSelection'] is True and row['settings']['sharedLearning']['enabled'] is True

def selection_root(cfg,project):
    from .shared import _safe_root
    if cfg.get('sharedLearning',{}).get('profile')!=SCHEMA:return None
    root=_safe_root(cfg,project)
    if root is None or not selection_enabled(root,project):return None
    return root

def activate(project,root,*,accepted_sha256,choice,mode,expires_at_unix_ms,
             enable_learning=False,staged_no_selection=False,fault=lambda _:None):
    root=resident._private_state(Path(root));project=Path(project).resolve(strict=True)
    # This same project settings lock is used by setup/install writers.
    import install_update
    with install_update._locked(project):
        fd=_lock(root/'learning-profile.lock')
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            path=root/INSTALL
            old=read_private_json(path) if path.exists() or path.is_symlink() else None
            if old and old['acceptedSha256']==accepted_sha256:
                row=old['proposal']
                if (digest(row)!=accepted_sha256 or (row['choice'],row['mode'],row['expiresAtUnixMs'])!=(choice,mode,expires_at_unix_ms)
                        or (enable_learning and row['settings']['contextLearning'].get('capture') is not True)
                        or (staged_no_selection and row['settings']['contextLearning'].get('liveSelection') is not False)):
                    raise ValueError('profile_acceptance_changed')
            else:
                row=proposal(project,root,choice=choice,mode=mode,expires_at_unix_ms=expires_at_unix_ms,
                    enable_learning=enable_learning,staged_no_selection=staged_no_selection)
                if accepted_sha256!=digest(row):raise ValueError('profile_acceptance_required')
            from .shared import _policy
            if row['projectRoot']!=str(project) or row['namespaceRoot']!=str(root):raise ValueError('profile_scope_invalid')
            _check_scope(row,root,_policy(root),'quality_evaluate',settings=False)
            current=_settings(project)
            if digest(current) not in (row['sourceSettingsSha256'],digest(row['settings'])):raise ValueError('profile_settings_cas_conflict')
            record={'acceptedSha256':accepted_sha256,'proposal':row,'phase':'intent'}
            _atomic_json(path,record);_sync(root);fault('intent')
            _atomic_json(project/'.auto-context/settings.json',row['settings']);_sync(project/'.auto-context')
            record['phase']='settings_written';_atomic_json(path,record);_sync(root);fault('settings_written')
            _atomic_json(root/NAME,{'acceptedSha256':accepted_sha256,'proposal':row});_sync(root);fault('profile_written')
            record['phase']='activated';_atomic_json(path,record);_sync(root)
            return {'status':'learning_profile_selected','acceptedSha256':accepted_sha256,'localTrain':row['localTrain'],'autoApply':row['autoApply'],'teacherPermission':'unchanged'}
        finally:os.close(fd)
