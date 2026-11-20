"""Exact accepted single-project local execution; teacher consent is separate."""
from __future__ import annotations
import hashlib,json,os,re,stat,time
from pathlib import Path
from memvane.runtime.api import prepared,preparation_checkpoint
from .store import canonical
from .dataset import read_private_json
from .adapter import EDIT_INPUT_VIEW,model_identity
from .inference_identity import code_identity,file_sha

NAME='project-learning-authorization.json'
SCHEMA='qmd-project-learning-authorization-v1'
SCOPE='single-project-local-learning'
COHORT_SCHEMA='qmd-project-capture-cohort-v1'
OPERATIONS=['gold','local_train','quality_evaluate']
LOCAL_OPERATIONS=['local_train','quality_evaluate']
DEFAULT_TEACHER_OPERATIONS=['gold_default','local_train','quality_evaluate']
TEACHER_ENABLE='project-teacher-enable.json'
MAX_LIFETIME_MS=14*86400000
DEFAULT_LIFETIME_MS=10*365*86400000
_MODEL_CACHE={}


def proposal_sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def single_host(operations):
    """The two default (selected-install, single-host) grant shapes."""
    return operations in (LOCAL_OPERATIONS,DEFAULT_TEACHER_OPERATIONS)


def present(root):
    p=Path(root)/NAME
    return p.exists() or p.is_symlink()


def _path(path,*,private=False,directory=False):
    p=Path(path)
    if not p.is_absolute() or any(q.is_symlink() for q in (p,*p.parents)):
        raise ValueError('project_scope_unsafe_path')
    info=p.stat()
    if (info.st_uid!=os.getuid() or (not directory and not stat.S_ISREG(info.st_mode))
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and info.st_nlink!=1)
            or info.st_mode & (0o077 if private else 0o022)):
        raise ValueError('project_scope_unsafe_path')
    return p.resolve(strict=True)


def _envelope(root):
    root=_path(root,private=True,directory=True)
    if os.path.lexists(root/'project-learning-renewal.json'):
        raise ValueError('project_scope_renewal_pending')
    if os.path.lexists(root/TEACHER_ENABLE):
        raise ValueError('project_scope_teacher_enable_pending')
    value=read_private_json(_path(root/NAME,private=True))
    if (not isinstance(value,dict) or set(value)!={'acceptedSha256','proposal'}
            or not isinstance(value['proposal'],dict)
            or value['acceptedSha256']!=proposal_sha256(value['proposal'])
            or value['proposal'].get('schema')!=SCHEMA
            or value['proposal'].get('namespaceRoot')!=str(root)):
        raise ValueError('project_scope_acceptance_invalid')
    row=value['proposal']
    local_only = single_host(row.get('operations'))
    if local_only and isinstance(row.get('projectRoot'), str):
        from memvane.learning.default_enrollment import authorized
        if not authorized(row.get('projectRoot'), root):
            raise ValueError('project_scope_authorization_invalid')
    elif local_only:
        raise ValueError('project_scope_authorization_invalid')
    lifetime = DEFAULT_LIFETIME_MS if local_only else MAX_LIFETIME_MS
    cohort=row.get('cohort',{})
    limits={'minTrainCases':5,'minQualityTrainCases':100,'minTrainFamilies':5,
            'minValidationCases':50,'minValidationFamilies':5,'minEmptyValidation':10}
    if (set(row)!={'schema','scope','projectRoot','namespaceRoot','operations','cohort','expiresAtUnixMs','binding'}
            or row['scope']!=SCOPE or row['operations'] not in (OPERATIONS, LOCAL_OPERATIONS, DEFAULT_TEACHER_OPERATIONS)
            or not isinstance(cohort,dict) or set(cohort)!=set(limits)|{'schema','id','notBeforeUnixMs'}
            or cohort.get('schema')!=COHORT_SCHEMA
            or any(type(cohort.get(k))is not int or cohort[k]!=v for k,v in limits.items())
            or not isinstance(cohort.get('id'),str) or not re.fullmatch('[a-z0-9][a-z0-9-]{2,63}',cohort['id'])
            or type(cohort.get('notBeforeUnixMs'))is not int
            or type(row['expiresAtUnixMs'])is not int
            or not 0<cohort['notBeforeUnixMs']<row['expiresAtUnixMs']<=cohort['notBeforeUnixMs']+lifetime):
        raise ValueError('project_scope_authorization_invalid')
    return row


def owner(root):
    return _path(_envelope(root)['projectRoot'],directory=True)


def _settings_scope(project):
    from memvane.settings import api as project_dir
    from .learning_profile import _settings
    raw=_settings(project)
    # The project writes minimal settings; the scope binds the collections they derive to.
    if not project_dir.is_minimal_settings(raw):
        raise ValueError('project_scope_settings_invalid')
    cfg=project_dir.normalize_config(raw,project_dir.folder(project).name)
    result={key:cfg.get(key) for key in ('indexing','collectionPaths','collections','allowRoots','skipPaths')}
    for name,path in result['collectionPaths'].items():
        if not isinstance(name,str) or not name or not isinstance(path,str):
            raise ValueError('project_scope_collections_invalid')
        target=project/path
        if not target.is_absolute() or any(p.is_symlink() for p in (target,*target.parents)):
            raise ValueError('project_scope_collections_unsafe')
        target.resolve(strict=False).relative_to(project)
    return result


def _installations(project=None):
    """Project-effective enabled roots from each official host CLI run in the
    project; disabled historical installs elsewhere in the account are ignored.
    Without a project, the previous account-global inventory is kept as is."""
    from .host_inventory import _discover_host_roots,_project_host_roots
    rows=[]
    if project is None:
        roots=_discover_host_roots(Path.home().resolve())
    else:
        try:
            roots=_project_host_roots(Path.home().resolve(),_path(project,directory=True))
        except ValueError as exc:
            raise ValueError('project_scope_installation_invalid') from exc
    for host in ('claude','codex'):
        paths=roots.get(host,[])
        if len(paths)!=1:raise ValueError('project_scope_installation_invalid')
        root=Path(paths[0]).resolve(strict=True)
        adapter=root/'memvane/learning/context_learning/adapter.py'
        if project is None:
            # Preserve the historical account-wide official-marketplace proof.
            from . import adapter_binding
            try:
                found=adapter_binding._official_cache(adapter,Path.home())
                adapter_binding._installed_current(*found[:3],Path.home(),None)
            except ValueError:
                adapter_binding._local_binding(adapter,Path.home(),None,execution=True)
        else:
            # The project resolver already verified the host CLI selection and
            # Claude's install record. The caller immediately checks every
            # installed source/hook byte in _installed_binding, including the
            # selected Codex cache. This permits an exact QA marketplace id
            # without treating an unselected account plugin as executable.
            _path(root,directory=True)
            _path(adapter)
        rows.append({'host':host,'root':str(root)})
    return rows


def _local_installations(project):
    """Bind the project-effective host executing this default enrollment."""
    from . import host_inventory
    from .selector_heal import running_adapter
    adapter=running_adapter();home=Path.home().resolve();root=adapter.parents[3]
    try:
        relative=adapter.relative_to(home).parts
    except ValueError as exc:
        raise ValueError('project_scope_installation_invalid') from exc
    host='codex' if relative and relative[0]=='.codex' else 'claude'
    resolver=host_inventory._project_codex_root if host=='codex' else host_inventory._project_claude_root
    try:
        selected=Path(resolver(home,_path(project,directory=True))).resolve(strict=True)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise ValueError('project_scope_installation_invalid') from exc
    if selected!=root:raise ValueError('project_scope_installation_invalid')
    return [{'host':host,'root':str(root)}]


def _code_snapshot(root,home):
    """Fresh owned bytes once per tree; never reuse trust across operations."""
    from .adapter_binding import _local_code
    fingerprint=_local_code(root,home)
    hashes=dict(fingerprint);files={};core=root/'memvane'
    for path in sorted(core.rglob('*.py')):
        preparation_checkpoint()
        relative=str(path.relative_to(core))
        value=hashes.get('memvane/'+relative)
        # _local_code excludes cache directories, but the existing Python
        # identity includes any .py source there. Keep that coverage too.
        files[relative]=file_sha(_path(path)) if value is None else value
    return files,fingerprint


def _installed_binding(rows, *, local_only=False):
    """Host inventories establish provenance; every operation rechecks bytes."""
    hosts=[r.get('host') for r in rows] if isinstance(rows,list) else []
    if hosts!=['claude','codex'] and not (local_only and hosts in (['claude'],['codex'])):
        raise ValueError('project_scope_installation_invalid')
    source=Path(__file__).parents[3]
    expected,current_fingerprint=_code_snapshot(source,source)
    result=[];versions=set();current=proposal_sha256(expected)
    for row in rows:
        preparation_checkpoint()
        if set(row)!={'host','root'}:raise ValueError('project_scope_installation_invalid')
        base=_path(row['root'],directory=True)
        host=row['host'];manifest=_path(base/('.'+host+'-plugin/plugin.json'))
        metadata=json.loads(manifest.read_text())
        if (metadata.get('name')!='memvane' or not isinstance(metadata.get('version'),str)):
            raise ValueError('project_scope_installation_invalid')
        files,fingerprint=_code_snapshot(base,Path.home())
        if files!=expected:raise ValueError('project_scope_code_changed')
        if fingerprint!=current_fingerprint:raise ValueError('project_scope_code_changed')
        versions.add(metadata['version'])
        result.append({**row,'manifestSha256':file_sha(manifest),
                       'adapterSha256':files['learning/context_learning/adapter.py'],
                       'version':metadata['version'],'codeSha256':current,
                       'executableSha256':proposal_sha256(fingerprint)})
    if len(versions)!=1:raise ValueError('project_scope_installation_changed')
    if _code_snapshot(source,source)!=(expected,current_fingerprint):
        raise ValueError('project_scope_code_changed')
    return result


def _registry_binding(project):
    """Small host controls, including absence; never hash the active state DB."""
    relatives=('.claude/settings.json','.claude/settings.local.json',
               '.claude/plugins/known_marketplaces.json','.claude/plugins/installed_plugins.json',
               '.codex/config.toml','.codex/plugins/installed_plugins.json',
               '.codex/plugins/known_marketplaces.json','.codex/plugins/marketplaces.json',
               '.codex/plugins/config.json')
    project_relatives=('.claude/settings.json','.claude/settings.local.json',
                       '.codex/config.toml')
    rows={}
    for key,path in ([(relative,Path.home()/relative) for relative in relatives]
                     + [('project/'+relative,Path(project)/relative)
                        for relative in project_relatives]):
        if any(p.is_symlink() for p in (path,*path.parents)):
            raise ValueError('project_scope_registry_unsafe')
        if not path.exists():rows[key]=None;continue
        path=_path(path)
        if path.stat().st_size>4*1024*1024:raise ValueError('project_scope_registry_invalid')
        before=_stat_identity(path);value=file_sha(path)
        if before!=_stat_identity(path):raise ValueError('project_scope_registry_changed')
        rows[key]=value
    return rows


def _stat_identity(path):
    info=Path(path).stat()
    return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _model_identity(root,model):
    """Full hash once per process/grant; changed metadata never renews trust."""
    from .adapter import MODEL_FILES
    paths=[_path(model/name) for name in MODEL_FILES]
    signature=tuple(_stat_identity(path) for path in paths)
    grant=Path(root)/NAME
    grant_identity=_stat_identity(grant) if grant.exists() else None
    key=(str(root),str(model))
    previous=_MODEL_CACHE.get(key)
    if previous is not None and previous[0]==grant_identity:
        if previous[1]!=signature:raise ValueError('project_scope_model_changed')
        return previous[2]
    value=model_identity(model)
    if signature!=tuple(_stat_identity(path) for path in paths):
        raise ValueError('project_scope_model_changed')
    _MODEL_CACHE[key]=(grant_identity,signature,value)
    return value


def _binding(project,root,policy,installs,*,local_only=False,policy_disk=None):
    project=_path(project,directory=True);root=_path(root,private=True,directory=True)
    if root==project or project in root.parents:raise ValueError('project_scope_namespace_invalid')
    settings=_settings_scope(project)
    for source in settings['collectionPaths'].values():
        base=(project/source).resolve(strict=False)
        if root==base or base in root.parents:raise ValueError('project_scope_namespace_invalid')
    disk=(read_private_json(_path(root/'shared-policy.json',private=True))
          if policy_disk is None else policy_disk)
    expected={key:value for key,value in policy.items() if not key.startswith('_')}
    registry=_registry_binding(project) if not local_only else None
    if local_only:
        if (not isinstance(installs,list) or len(installs)!=1
                or not isinstance(installs[0],dict) or set(installs[0])!={'host','root'}):
            raise ValueError('project_scope_installation_invalid')
        from .host_self import selection
        selected,registry=selection(project,host=installs[0]['host'])
        if selected!=installs:raise ValueError('project_scope_installation_changed')
    # Shared workers resolve the equivalent installed host adapter in memory.
    installed=_installed_binding(installs,local_only=True) if local_only else _installed_binding(installs)
    adapters={str(Path(r['root'])/'memvane/learning/context_learning/adapter.py') for r in installed}
    executing_adapters=adapters
    if local_only:
        from .selector_heal import running_adapter
        current=(installed if str(running_adapter()) in adapters else
                 _installed_binding(_local_installations(project),local_only=True))
        executing_adapters=adapters | {str(Path(r['root'])/'memvane/learning/context_learning/adapter.py') for r in current}
    if (not isinstance(expected.get('trainerArgv'),list) or len(expected['trainerArgv'])!=3
            or len(disk.get('trainerArgv',[]))!=3
            or expected['trainerArgv'][1] not in executing_adapters or disk['trainerArgv'][1] not in adapters):
        raise ValueError('project_scope_adapter_changed')
    expected['trainerArgv']=list(expected['trainerArgv']);expected['trainerArgv'][1]=disk['trainerArgv'][1]
    if expected!=disk or disk.get('modelInputView')!=EDIT_INPUT_VIEW:
        raise ValueError('project_scope_policy_changed')
    python=_path(Path(disk['trainerArgv'][0]).resolve(strict=True));model=_path(disk['trainerArgv'][2],directory=True)
    from .adapter import MODEL_FILES
    for name in MODEL_FILES:_path(model/name)
    policy_sha=(file_sha(root/'shared-policy.json') if policy_disk is None
                else hashlib.sha256((canonical(disk)+'\n').encode()).hexdigest())
    return {'settingsScope':settings,'hostRegistry':registry,'policySha256':policy_sha,
            'installations':installed,'codeSha256':code_identity(),
            'pythonPath':disk['trainerArgv'][0],'pythonResolvedPath':str(python),'pythonSha256':file_sha(python),
            'modelDir':str(model),'modelIdentitySha256':_model_identity(root,model),
            'baseArtifactSha256':file_sha(_path(disk['baseArtifact'])),
            'modelInputView':EDIT_INPUT_VIEW}


@prepared
def proposal(project,root,policy,*,expires_at_unix_ms,not_before_unix_ms,cohort_id='general-v1',local_only=False,
             default_teacher=False):
    project=_path(project,directory=True);root=_path(root,private=True,directory=True)
    if type(local_only) is not bool or type(default_teacher) is not bool:
        raise ValueError('project_scope_authorization_invalid')
    operations=DEFAULT_TEACHER_OPERATIONS if default_teacher else LOCAL_OPERATIONS if local_only else OPERATIONS
    local_only=single_host(operations)
    if local_only:
        from memvane.learning.default_enrollment import authorized
        if not authorized(project,root):raise ValueError('project_scope_authorization_invalid')
    lifetime=DEFAULT_LIFETIME_MS if local_only else MAX_LIFETIME_MS
    if (type(expires_at_unix_ms)is not int or type(not_before_unix_ms)is not int
            or not 0<not_before_unix_ms<expires_at_unix_ms<=not_before_unix_ms+lifetime
            or not time.time()*1000<expires_at_unix_ms<=time.time()*1000+lifetime):
        raise ValueError('project_scope_expiry_invalid')
    if not isinstance(cohort_id,str) or not re.fullmatch('[a-z0-9][a-z0-9-]{2,63}',cohort_id):
        raise ValueError('project_scope_cohort_invalid')
    cohort={'schema':COHORT_SCHEMA,'id':cohort_id,'notBeforeUnixMs':not_before_unix_ms,
            'minTrainCases':5,'minQualityTrainCases':100,'minTrainFamilies':5,
            'minValidationCases':50,'minValidationFamilies':5,'minEmptyValidation':10}
    binding=(_binding(project,root,policy,_local_installations(project),local_only=True)
             if local_only else _binding(project,root,policy,_installations(project)))
    return {'schema':SCHEMA,'scope':SCOPE,'projectRoot':str(project),'namespaceRoot':str(root),
            'operations':operations,'cohort':cohort,'expiresAtUnixMs':expires_at_unix_ms,
            'binding':binding}


def activate(project,root,policy,*,accepted_sha256,expires_at_unix_ms,not_before_unix_ms,cohort_id='general-v1',local_only=False,
             default_teacher=False):
    value=proposal(project,root,policy,expires_at_unix_ms=expires_at_unix_ms,
                   not_before_unix_ms=not_before_unix_ms,cohort_id=cohort_id,local_only=local_only,
                   default_teacher=default_teacher)
    if accepted_sha256!=proposal_sha256(value):raise ValueError('project_scope_acceptance_required')
    path=Path(root)/NAME
    if path.exists() or path.is_symlink():
        if _envelope(root)==value:return {'status':'already_active','acceptedSha256':accepted_sha256}
        raise ValueError('project_scope_existing_differs')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        raw=(canonical({'acceptedSha256':accepted_sha256,'proposal':value})+'\n').encode()
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    except BaseException:
        raise
    from .training_cursor import _sync
    _sync(Path(root))
    return {'status':'activated','acceptedSha256':accepted_sha256}


@prepared
def require(root,policy,operation,*,fresh_inventory=False):
    if operation not in OPERATIONS and operation!='gold_default':raise ValueError('project_scope_operation_invalid')
    value=_envelope(root)
    if operation not in value['operations']:raise ValueError('project_scope_operation_invalid')
    if time.time()*1000>=value['expiresAtUnixMs']:raise ValueError('project_scope_expired')
    project=_path(value['projectRoot'],directory=True)
    if type(fresh_inventory)is not bool:raise ValueError('project_scope_inventory_invalid')
    installs=[{k:r[k] for k in ('host','root')} for r in value['binding']['installations']]
    # A directory-source Claude marketplace can change its source pointer
    # without changing the account registry or the previously hashed root.
    # Re-resolve the project-effective host inventory on every grant use.
    local_only=single_host(value['operations'])
    if not local_only and _installations(project)!=installs:
        raise ValueError('project_scope_installation_changed')
    binding=(_binding(project,Path(root),policy,installs,local_only=True)
             if local_only else _binding(project,Path(root),policy,installs))
    if value['binding']!=binding:
        raise ValueError('project_scope_binding_changed')
    if operation=='gold':
        if not (Path(root)/'project-teacher-consent.json').exists():
            raise ValueError('project_scope_consent_required')
        from .general_teacher import require_consent
        require_consent(Path(root),project)
    elif operation=='gold_default':
        # Only the default teacher's v2 consent grants this operation.
        from .teacher_consent import require_consent_v2
        require_consent_v2(Path(root),project)
    return value
