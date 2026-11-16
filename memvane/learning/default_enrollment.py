"""Durable, project-scoped default learning intent for selected installs."""
import hashlib
import json
import os
import stat
from pathlib import Path

from memvane.learning.context_learning import learning_profile
from memvane.learning.context_learning.dataset import read_private_json
from memvane.learning.context_learning.local_cycle import _atomic_json
from memvane.learning.context_learning.selector_heal import namespace

NAME = 'default-learning.json'
SCHEMA = 'memvane-default-learning-v1'
BINDING = 'default-learning-binding.json'


def default_namespace(project, install_id):
    from memvane.learning.context_learning.runtime_setup import learning_state_base
    scope = json.dumps([str(project), install_id], separators=(',', ':'), ensure_ascii=False)
    return learning_state_base() / 'live-selection' / hashlib.sha256(scope.encode()).hexdigest()


def validate_preferences(raw):
    from memvane.settings import api as config
    if not config.is_minimal_settings(raw) or config.normalize_config(raw).get('learningSettingsInvalid'):
        raise ValueError('setup_learning_disabled')
    flags=raw.get('contextLearning',{});shared=raw.get('sharedLearning',{})
    if (flags.get('capture') is False or flags.get('autoCycle') is False
            or shared.get('enabled') is False or shared.get('autoApply') is False):
        raise ValueError('setup_learning_disabled')


def desired_settings(raw, root, *, mode='default'):
    from memvane.learning.project_learning_setup import DEFAULT_LOCAL, SYNTHETIC_STAGED
    if mode == DEFAULT_LOCAL:
        validate_preferences(raw)
    settings, dropped = learning_profile.closed_learning(raw)
    learning = settings.setdefault('contextLearning', {})
    shared = settings.setdefault('sharedLearning', {})
    if not isinstance(learning,dict) or not isinstance(shared,dict):raise ValueError('setup_settings_invalid')
    if shared.get('root') not in (None,str(root)):raise ValueError('setup_namespace_changed')
    if settings.get('indexing') is False:raise ValueError('setup_indexing_disabled')
    learning.update(capture=True,autoCycle=True)
    learning.pop('liveSelection',None)
    shared.update(enabled=True,root=str(root),profile=learning_profile.SCHEMA,autoApply=mode!=SYNTHETIC_STAGED)
    return settings,dropped


def default_local_only(root, project, policy):
    """Accept only the selected install's intact teacher-free default setup."""
    from memvane.learning import project_learning_setup as setup
    root=Path(root);path=root/setup.NAME
    if not path.exists() and not path.is_symlink():return False
    row=read_private_json(setup._path(path,private=True))
    plan=row.get('plan') if isinstance(row,dict) else None
    if not isinstance(plan,dict) or plan.get('mode')!=setup.DEFAULT_LOCAL:return False
    expected={k:v for k,v in policy.items() if not k.startswith('_')}
    if (isinstance(expected.get('trainerArgv'),list) and len(expected['trainerArgv'])==3
            and isinstance(plan.get('policy',{}).get('trainerArgv'),list)):
        expected['trainerArgv']=list(expected['trainerArgv'])
        expected['trainerArgv'][1]=plan['policy']['trainerArgv'][1]
    if (row.get('phase')!='activated' or row.get('acceptedSha256')!=setup.proposal_sha256(plan)
            or plan.get('projectRoot')!=str(Path(project).resolve())
            or plan.get('namespaceRoot')!=str(root) or plan.get('policy')!=expected
            or plan.get('teacherConsent') is not None
            or os.path.lexists(root/'project-teacher-consent.json')
            or not authorized(project,root)):
        raise ValueError('setup_default_scope_changed')
    return True


def authorized(project, root):
    """The intent belongs to the currently selected installation."""
    from memvane.journal.api import select_route, verify_selected_route_for_write
    project, root = Path(project).resolve(strict=True), Path(root)
    decision = select_route(str(project), action='update')
    proof = verify_selected_route_for_write(decision)
    install_id = decision['installId']
    if proof['installId'] != install_id or root != default_namespace(project, install_id):
        return False
    state = Path(decision['journalPath']).parent
    intent = read_private_json(state / NAME)
    binding = read_private_json(root / BINDING)
    expected = {'schema': SCHEMA, 'projectRoot': str(project), 'installId': install_id,
                'namespaceRoot': str(root), 'choice': 'local-auto-apply'}
    return intent == expected and binding == expected


def record(project, state, install_id):
    project = Path(project).resolve(strict=True)
    state = learning_profile.resident._private_state(Path(state))
    raw = learning_profile._settings(project)
    from memvane.settings import api as settings
    if not settings.is_minimal_settings(raw):
        return {'status': 'disabled'}
    if settings.normalize_config(raw, project.name).get('learningSettingsInvalid'):
        return {'status': 'disabled'}
    learning = raw.get('contextLearning', {})
    shared = raw.get('sharedLearning', {})
    if (learning.get('capture') is False or learning.get('autoCycle') is False
            or shared.get('enabled') is False):
        return {'status': 'disabled'}
    if shared.get('autoApply') is False:
        return {'status': 'preserved'}
    existing = shared.get('root')
    own_root = default_namespace(project, install_id)
    if existing and learning_profile.present(existing):
        if Path(existing) != own_root:
            return {'status': 'preserved'}
        journal = own_root / 'project-learning-setup.json'
        if not journal.exists() or read_private_json(journal).get('phase') == 'activated':
            return {'status': 'preserved'}
    if learning_profile.present(namespace(project)):
        return {'status': 'preserved'}
    path = state / NAME
    row = {'schema': SCHEMA, 'projectRoot': str(project), 'installId': install_id,
           'namespaceRoot': str(own_root), 'choice': 'local-auto-apply'}
    if path.exists() or path.is_symlink():
        if read_private_json(path) != row:
            raise ValueError('default_learning_scope_changed')
    else:
        _atomic_json(path, row)
    return {'status': 'pending_runtime', 'choice': row['choice']}


def _prepare_namespace(root, executable, model):
    from memvane.learning.context_learning import adapter, selector_heal
    from memvane.learning.context_learning.inference_identity import file_sha
    from memvane.learning.context_learning.runtime_setup import learning_state_base
    root = Path(root)
    if not root.is_absolute() or any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError('default_learning_namespace_unsafe')
    state_base = learning_state_base()
    if root.parent != state_base / 'live-selection':
        raise ValueError('default_learning_namespace_unsafe')
    for directory in (state_base, root.parent, root):
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('default_learning_namespace_unsafe')
    base = root / 'base.artifact'
    if not os.path.lexists(base):
        adapter.prepare_base_artifact(model, base)
    expected_base = {'schema_version': 1, 'kind': 'laya_base_head',
                     'base_model_sha256': adapter.model_identity(model),
                     'adapter_version': adapter.ADAPTER_VERSION}
    if read_private_json(base) != expected_base:
        raise ValueError('default_learning_base_changed')
    rows = {
        'live-selector.json': {'schema': 'qmd-live-selector-v1',
                               'trainerArgv': [executable, str(selector_heal.running_adapter()), model],
                               'timeoutSeconds': 30, 'maxRssMib': 4096},
        'active-checkpoint.json': {'schema_version': 1, 'artifact_path': str(base), 'artifact_sha256': file_sha(base),
                                   'cycle_id': 'base', 'model_input_view': adapter.EDIT_INPUT_VIEW},
    }
    for name, value in rows.items():
        target = root / name
        if os.path.lexists(target):
            current = read_private_json(target)
            if current != value:
                argv = current.get('trainerArgv') if isinstance(current, dict) else None
                if (name == 'live-selector.json' and isinstance(argv, list) and len(argv) == 3
                        and {**current, 'trainerArgv': [argv[0], value['trainerArgv'][1], argv[2]]} == value):
                    # Reuse heal's content/approved-scope checks for a moved anchor.
                    # Accepted policies stay intact; only rebind may rewrite them.
                    selector_heal.rebind(root)
                    continue
                legacy = {key: item for key, item in value.items() if key != 'schema_version'}
                if name != 'active-checkpoint.json' or current != legacy:
                    raise ValueError('default_learning_namespace_changed')
                _atomic_json(target, value)
        else:
            _atomic_json(target, value)


def _activate_profile(project, root):
    import time
    from memvane.learning import project_learning_setup as setup
    journal = Path(root) / setup.NAME
    if journal.exists() or journal.is_symlink():
        row = read_private_json(journal)
        if row.get('phase') == 'activated':
            if not (Path(root) / learning_profile.NAME).is_file():
                raise ValueError('default_learning_profile_missing')
            return
        if row.get('phase') != 'recovered':
            setup.recover(project, root)
    if learning_profile.present(root):
        raise ValueError('default_learning_existing_profile')
    now = int(time.time() * 1000)
    lifetime = 10 * 365 * 86400000 - 60000
    plan = setup.prepare(project, root, expires_at_unix_ms=now + lifetime,
                         not_before_unix_ms=now, mode=setup.DEFAULT_LOCAL)
    setup.activate(plan, accepted_sha256=setup.proposal_sha256(plan))


def prepare(project, state, install_id, *, lease_fd):
    from memvane.learning.context_learning import selector_heal
    choice = record(project, state, install_id)
    if choice['status'] != 'pending_runtime':
        return choice
    try:
        executable, model = selector_heal.official_runtime()
    except selector_heal.HealRefused as exc:
        if str(exc) == 'learning_runtime_not_installed':
            return choice
        raise
    if model is None:
        return choice
    project = Path(project).resolve(strict=True)
    root = default_namespace(project, install_id)
    policy = {'trainerArgv': [executable, str(selector_heal.running_adapter()), model], 'maxRssMib': 4096}
    reason = selector_heal.pressure(policy)
    if reason is not None:
        raise selector_heal.HealDeferred(reason)
    _prepare_namespace(root, executable, model)
    intent = read_private_json(Path(state) / NAME)
    binding = root / BINDING
    if os.path.lexists(binding):
        if read_private_json(binding) != intent:
            raise ValueError('default_learning_scope_changed')
    else:
        _atomic_json(binding, intent)
    from memvane.learning import project_learning_setup as setup
    journal = root / setup.NAME
    if os.path.lexists(journal):
        phase = read_private_json(journal).get('phase')
        if phase not in ('activated', 'recovered'):
            setup.recover(project, root)
    result = selector_heal.heal(project, state=root, _lease_fd=lease_fd)
    if result.get('status') not in ('selector_ready', 'selector_prepared'):
        return result
    warm = selector_heal.warm(project, state=root)
    if warm.get('status') not in ('selector_warm', 'selector_warm_current'):
        return warm
    _activate_profile(project, root)
    return {'status': 'selected', 'warm': warm['status']}
