"""Default-local -> default-teacher: the one transaction that widens a default grant.

Selector maintenance runs it after renewal, heal and warm, inside the prepare
lease. It never asks; the setup skill's ``restore-teacher-enable`` is its only
manual exit. Every write is a compare-and-swap against the bytes read under the
same locks as renewal, and an intent file fences every new-bundle consumer
(``project_scope._envelope``, ``learning_profile.require``/``selection_enabled``)
until the transaction completes or the old bytes are back. The grant is written
first so an older bundle stops at its own ``_envelope`` before any other change.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path

from memvane.learning import default_enrollment as enrollment, project_learning_setup as setup
from memvane.learning.context_learning import learning_profile as profile, project_scope as scope
from memvane.learning.context_learning import teacher_consent, teacher_state
from memvane.learning.context_learning.dataset import read_private_json
from memvane.learning.context_learning.shared import _project_key
from memvane.learning.context_learning.store import canonical
from memvane.learning.context_learning.training_cursor import _sync
from memvane.learning.project_learning_recovery import _recovery_read, _recovery_target, _restore_bytes
from memvane.kernel.memvane_version import version_key

SCHEMA = 'memvane-default-teacher-enable-v1'
INTENT = teacher_state.ENABLE_INTENT
GRANTED_BY = 'default-enrollment-upgrade'
PINNED = ('shared-policy.json', 'live-selector.json')
INTENT_KEYS = frozenset(('schema', 'phase', 'projectRoot', 'namespaceRoot', 'installId', 'host',
                         'hostVersion', 'settingsSha256', 'grantedAtUnixMs', 'old', 'new'))


def _shared(project):
    return 'consents/' + _project_key(project) + '.json'


def writes(project):
    """Write order. Rollback runs it backwards, so the grant is restored last."""
    return (scope.NAME, teacher_consent.NAME, _shared(project), setup.NAME,
            profile.NAME, profile.INSTALL)


def targets(project):
    return PINNED + writes(project)


def raw(value):
    return (canonical(value) + '\n').encode()


def sha(data):
    import hashlib
    return None if data is None else hashlib.sha256(data).hexdigest()


def _without(value, keys):
    return {key: item for key, item in value.items() if key not in keys}


def _read(root, relative):
    path = Path(root) / relative
    return _recovery_read(path) if os.path.lexists(path) else None


def _read_all(root, project):
    return {name: _read(root, name) for name in targets(project)}


def _json(data):
    try:
        return json.loads(data)
    except (TypeError, ValueError) as exc:
        raise ValueError('teacher_enable_source_invalid') from exc


def _envelope(data, digest):
    value = _json(data)
    if not isinstance(value, dict) or set(value) != {'acceptedSha256', 'proposal'}:
        raise ValueError('teacher_enable_source_invalid')
    row = value['proposal']
    if not isinstance(row, dict) or value['acceptedSha256'] != digest(row):
        raise ValueError('teacher_enable_source_invalid')
    return row


def target(project, root, old, granted_at_unix_ms):
    """New bytes from the old bytes alone; a pure function of its arguments."""
    project, root = Path(project), Path(root)
    shared = _shared(project)
    if set(old) != set(targets(project)) or old[teacher_consent.NAME] is not None:
        raise ValueError('teacher_enable_source_invalid')
    grant = _envelope(old[scope.NAME], scope.proposal_sha256)
    accepted = _envelope(old[profile.NAME], profile.digest)
    install = _json(old[profile.INSTALL])
    local = _json(old[shared])
    journal = _json(old[setup.NAME])
    plan = journal.get('plan') if isinstance(journal, dict) else None
    expected_local = {'schema': 'qmd-shared-consent-v1', 'projectRoot': str(project),
                      'sharedRoot': str(root), 'capture': True, 'localTraining': True,
                      'externalTeacher': False}
    if (grant.get('operations') != scope.LOCAL_OPERATIONS
            or install != {'acceptedSha256': profile.digest(accepted), 'proposal': accepted,
                           'phase': 'activated'}
            or local != expected_local or type(local.get('externalTeacher')) is not bool
            or not isinstance(plan, dict) or plan.get('mode') != setup.DEFAULT_LOCAL
            or plan.get('teacherConsent') is not None or plan.get('localConsent') != local
            or journal.get('phase') != 'activated'
            or not isinstance(journal.get('written'), dict)):
        raise ValueError('teacher_enable_source_invalid')
    new_grant = {**grant, 'operations': list(scope.DEFAULT_TEACHER_OPERATIONS)}
    grant_raw = raw({'acceptedSha256': scope.proposal_sha256(new_grant), 'proposal': new_grant})
    consent = teacher_consent.proposal(project, granted_by=GRANTED_BY,
                                       granted_at_unix_ms=granted_at_unix_ms)
    consent_raw = raw(teacher_consent.envelope(consent))
    new_local = {**local, 'externalTeacher': True}
    local_raw = raw(new_local)
    new_journal = deepcopy(journal)
    new_plan = new_journal['plan']
    new_plan.update(mode=setup.DEFAULT_TEACHER, teacherConsent=consent, localConsent=new_local)
    new_journal['acceptedSha256'] = setup.proposal_sha256(new_plan)
    new_journal['written'].update({scope.NAME: sha(grant_raw), teacher_consent.NAME: sha(consent_raw),
                                   shared: sha(local_raw)})
    new_journal['configurationPaths'] = setup.configuration_paths(project, setup.DEFAULT_TEACHER)
    new_profile = {**accepted, 'runtimeApprovalSha256': scope.proposal_sha256(new_grant)}
    envelope = {'acceptedSha256': profile.digest(new_profile), 'proposal': new_profile}
    return {'shared-policy.json': old['shared-policy.json'], 'live-selector.json': old['live-selector.json'],
            scope.NAME: grant_raw, teacher_consent.NAME: consent_raw, shared: local_raw,
            setup.NAME: raw(new_journal), profile.NAME: raw(envelope),
            profile.INSTALL: raw({**envelope, 'phase': 'activated'})}


def prove_narrow(project, old, new, granted_at_unix_ms):
    """The only widening is grant operations, the v2 consent and the shared flag."""
    project = Path(project)
    shared = _shared(project)
    before, after = _json(old[scope.NAME])['proposal'], _json(new[scope.NAME])['proposal']
    old_profile, new_profile = _json(old[profile.NAME])['proposal'], _json(new[profile.NAME])['proposal']
    old_journal, new_journal = _json(old[setup.NAME]), _json(new[setup.NAME])
    keep = ('profileProposal', 'targetSettingsSha256', 'sourceSettingsSha256', 'backupRoot', 'phase', 'schema')
    expected_consent = teacher_consent.envelope(teacher_consent.proposal(
        project, granted_by=GRANTED_BY, granted_at_unix_ms=granted_at_unix_ms))
    if (any(new[name] != old[name] for name in PINNED)
            or _without(before, {'operations'}) != _without(after, {'operations'})
            or before.get('operations') != scope.LOCAL_OPERATIONS
            or after.get('operations') != scope.DEFAULT_TEACHER_OPERATIONS
            or _without(old_profile, {'runtimeApprovalSha256'}) != _without(new_profile, {'runtimeApprovalSha256'})
            or _json(new[profile.INSTALL]) != {**_json(new[profile.NAME]), 'phase': 'activated'}
            or any(old_journal.get(key) != new_journal.get(key) for key in keep)
            or set(old_journal) != set(new_journal)
            or any(old_journal['written'].get(name) != new_journal['written'].get(name)
                   for name in (profile.NAME, profile.INSTALL, 'shared-policy.json'))
            or _without(old_journal['plan'], {'mode', 'teacherConsent', 'localConsent'})
            != _without(new_journal['plan'], {'mode', 'teacherConsent', 'localConsent'})
            or set(new_journal['written']) != set(old_journal['written']) | {teacher_consent.NAME}
            or new_journal.get('configurationPaths') != setup.configuration_paths(project, setup.DEFAULT_TEACHER)
            or old[teacher_consent.NAME] is not None
            or _json(new[teacher_consent.NAME]) != expected_consent
            or _without(_json(old[shared]), {'externalTeacher'}) != _without(_json(new[shared]), {'externalTeacher'})):
        raise ValueError('teacher_enable_widened')


def _pack(values):
    return {name: {'sha256': sha(data), 'bytes': None if data is None else base64.b64encode(data).decode('ascii')}
            for name, data in values.items()}


def _unpack(project, value):
    if not isinstance(value, dict) or set(value) != set(targets(project)):
        raise ValueError('teacher_enable_intent_invalid')
    result = {}
    for name, row in value.items():
        if not isinstance(row, dict) or set(row) != {'sha256', 'bytes'}:
            raise ValueError('teacher_enable_intent_invalid')
        data = None if row['bytes'] is None else base64.b64decode(row['bytes'], validate=True)
        if sha(data) != row['sha256']:
            raise ValueError('teacher_enable_intent_invalid')
        result[name] = data
    return result


def _intent(project, root, install_id=None):
    value = read_private_json(setup._path(Path(root) / INTENT, private=True))
    if (not isinstance(value, dict) or set(value) != INTENT_KEYS or value['schema'] != SCHEMA
            or value['phase'] not in ('intent', 'rolling_back')
            or value['projectRoot'] != str(project) or value['namespaceRoot'] != str(root)
            or (install_id is not None and value['installId'] != install_id)
            or value['host'] not in ('claude', 'codex') or version_key(value['hostVersion']) is None
            or type(value['grantedAtUnixMs']) is not int or value['grantedAtUnixMs'] <= 0):
        raise ValueError('teacher_enable_intent_invalid')
    return value, _unpack(project, value['old']), _unpack(project, value['new'])


def _put(root, relative, data, current):
    """One CAS write (or removal) against ``current``; read back because it is the only effect."""
    path = Path(root) / relative
    if data is None:
        _recovery_target(path, sha(current))
        setup._path(path, private=True).unlink()
        _sync(path.parent)
    else:
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700)
        _restore_bytes(path, data, expected_sha256=sha(current))
    if _read(root, relative) != data:
        raise ValueError('teacher_enable_write_unverified')


def _drop(root):
    (Path(root) / INTENT).unlink()
    _sync(Path(root))


def _mixed(current, old, new):
    return [name for name in current if current[name] not in (old[name], new[name])]


def _forward(project, root, old, new, fault):
    for name in writes(project):
        current = _read(root, name)
        if current not in (old[name], new[name]):
            raise ValueError('teacher_enable_mixed_state')
        if current != new[name]:
            _put(root, name, new[name], current)
        fault(name)


def _rollback(project, root, intent, old, new, fault=lambda _: None):
    if intent['phase'] != 'rolling_back':
        intent = {**intent, 'phase': 'rolling_back'}
        setup._write(Path(root) / INTENT, intent)
    for name in reversed(writes(project)):
        current = _read(root, name)
        if current not in (old[name], new[name]):
            raise ValueError('teacher_enable_mixed_state')
        if current != old[name]:
            _put(root, name, old[name], current)
        fault('rollback:' + name)
    if _read_all(root, project) != old:
        raise ValueError('teacher_enable_mixed_state')
    _drop(root)


def _blocked(root, project):
    """A precondition that defers the transaction, or None (cheap checks only)."""
    if os.path.lexists(Path(root) / profile.RENEWAL):
        return 'renewal_pending'
    if teacher_state.teacher_off(root):
        return 'teacher_off'
    from memvane.settings import api as project_dir
    if project_dir.settings_path(project) is None:
        return 'settings_layout'
    # No product code issues a learning-state move request, so none is judged here.
    return None


def _settings_sha(project):
    from memvane.learning.context_learning.inference_identity import file_sha
    return file_sha(setup._project_settings(project))


def _complete(project, root, install_id, intent, old, new, fault):
    _forward(project, root, old, new, fault)
    current = _read_all(root, project)
    if current != new:
        raise ValueError('teacher_enable_mixed_state')
    prove_narrow(project, old, current, intent['grantedAtUnixMs'])
    policy = json.loads(new['shared-policy.json'])
    if not enrollment.default_teacher(root, project, policy) or not enrollment.authorized(project, root):
        raise ValueError('teacher_enable_verification_failed')
    _drop(root)
    return {'status': 'enabled', 'grantedBy': GRANTED_BY}


def _resume(project, root, install_id, host_version, fault):
    intent, old, new = _intent(project, root, install_id)
    running = version_key(host_version)
    if running is None or running < version_key(intent['hostVersion']):
        return {'status': 'deferred', 'reason': 'owner_newer'}
    if _mixed(_read_all(root, project), old, new):
        raise ValueError('teacher_enable_mixed_state')
    if intent['phase'] == 'rolling_back':
        _rollback(project, root, intent, old, new, fault)
        return {'status': 'rolled_back', 'reason': 'teacher_enable_rolled_back'}
    reason = _blocked(root, project)
    if reason is None:
        try:
            if (_settings_sha(project) != intent['settingsSha256']
                    or not enrollment.authorized(project, root)):
                reason = 'teacher_enable_inputs_changed'
            elif target(project, root, old, intent['grantedAtUnixMs']) != new:
                reason = 'teacher_enable_target_changed'
        except (OSError, ValueError, KeyError, TypeError):
            reason = 'teacher_enable_inputs_changed'
    if reason is not None:
        _rollback(project, root, intent, old, new, fault)
        return {'status': 'rolled_back', 'reason': reason}
    try:
        return _complete(project, root, install_id, intent, old, new, fault)
    except ValueError as exc:
        if str(exc) == 'teacher_enable_mixed_state':
            raise
        _rollback(project, root, intent, old, new, fault)
        raise


def _start(project, root, install_id, host, host_version, fault, now):
    reason = _blocked(root, project)
    if reason is not None:
        return {'status': 'deferred', 'reason': reason}
    policy = read_private_json(setup._path(Path(root) / 'shared-policy.json', private=True))
    if not enrollment.default_local_only(root, project, policy):
        return {'status': 'not_applicable', 'kind': enrollment.profile_kind(root, project, policy)}
    # The grant must already bind the executing bundle (renewal current or renewed).
    grant = scope.require(root, policy, 'local_train', fresh_inventory=True)
    bound = grant['binding']['installations'][0]['version']
    if version_key(host_version) is None or version_key(host_version) < version_key(bound):
        return {'status': 'deferred', 'reason': 'owner_newer'}
    if grant['binding']['installations'][0]['version'] != host_version:
        return {'status': 'deferred', 'reason': 'bundle_not_bound'}
    old = _read_all(root, project)
    granted_at = int(now() * 1000)
    new = target(project, root, old, granted_at)
    prove_narrow(project, old, new, granted_at)
    intent = {'schema': SCHEMA, 'phase': 'intent', 'projectRoot': str(project),
              'namespaceRoot': str(root), 'installId': install_id, 'host': host,
              'hostVersion': host_version, 'settingsSha256': _settings_sha(project),
              'grantedAtUnixMs': granted_at, 'old': _pack(old), 'new': _pack(new)}
    setup._write(Path(root) / INTENT, intent)
    fault('intent')
    try:
        return _complete(project, root, install_id, intent, old, new, fault)
    except ValueError as exc:
        if str(exc) == 'teacher_enable_mixed_state':
            raise
        _rollback(project, root, intent, old, new)
        raise


class _Locks:
    """Renewal's lock order, plus the project worker lock without waiting."""

    def __init__(self, project, root):
        self.project, self.root, self.stack = project, root, None

    def __enter__(self):
        from contextlib import ExitStack
        from memvane.runtime.api import coordination_lock
        from memvane.settings.api import settings_lock
        stack = ExitStack()
        try:
            stack.enter_context(setup._locked(self.root))
            stack.enter_context(settings_lock(self.project))
            profile_fd = coordination_lock(self.root / 'learning-profile.lock')
            stack.callback(os.close, profile_fd)
            fcntl.flock(profile_fd, fcntl.LOCK_EX)
            worker = coordination_lock(self.root / 'project-worker.lock')
            stack.callback(os.close, worker)
            try:
                fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                held = False
        except BaseException:
            stack.close()
            raise
        self.stack = stack
        return held

    def __exit__(self, *exc):
        self.stack.close()
        return False


def _namespace(project, root, install_id):
    project = Path(project).resolve(strict=True)
    root = setup._path(root, private=True, directory=True)
    if install_id is not None and root != enrollment.default_namespace(project, install_id):
        raise ValueError('teacher_enable_namespace_invalid')
    return project, root


def run(root, project, *, host, host_version, install_id, fault=lambda _: None, now=time.time):
    """Enable the default teacher, or finish/undo an interrupted enable.

    Returns ``status`` ``enabled`` | ``deferred`` | ``rolled_back`` |
    ``not_applicable``; raises ``ValueError`` on a refusal (a mixed state keeps
    the intent and its fence and writes nothing).
    """
    project, root = _namespace(project, root, install_id)
    if host not in ('claude', 'codex'):
        raise ValueError('teacher_enable_host_invalid')
    with _Locks(project, root) as worker_free:
        if not worker_free:
            return {'status': 'deferred', 'reason': 'worker_busy'}
        if os.path.lexists(root / INTENT):
            return _resume(project, root, install_id, host_version, fault)
        return _start(project, root, install_id, host, host_version, fault, now)


def _state(current, old, new):
    return 'old' if current == old else 'new' if current == new else 'other'


def restore(root, project, *, plan_only=False):
    """Put every target back to the intent's old bytes and drop the intent.

    Explicit request only. A target that is neither old nor new is overwritten
    with a CAS on its current bytes. ``plan_only`` reports and writes nothing.
    """
    project, root = _namespace(project, root, None)
    if not os.path.lexists(root / INTENT):
        if plan_only:
            return {'status': 'no_enable_intent', 'changesApplied': 0}
        # Requests stopped after repeated failures resume with the next maintenance.
        return {'status': 'no_enable_intent', 'changesApplied': 0, 'requestsReset': reset_failures(root)}
    intent, old, new = _intent(project, root)
    current = _read_all(root, project)
    rows = [{'target': name, 'state': _state(current[name], old[name], new[name])}
            for name in targets(project)]
    if plan_only:
        return {'status': 'restore_planned', 'targets': rows, 'changesApplied': 0}
    with _Locks(project, root) as worker_free:
        if not worker_free:
            return {'status': 'deferred', 'reason': 'worker_busy', 'changesApplied': 0}
        intent, old, new = _intent(project, root)
        order = [name for name in reversed(writes(project)) if name != scope.NAME] + list(PINNED) + [scope.NAME]
        changed = 0
        for name in order:
            data = _read(root, name)
            if data != old[name]:
                _put(root, name, old[name], data)
                changed += 1
        if _read_all(root, project) != old:
            raise ValueError('teacher_enable_mixed_state')
        _drop(root)
    # Reported, not raised: the restore itself already happened.
    reset = reset_failures(root)
    record(project, 'enable_restored', None)
    return {'status': 'restored', 'targets': rows, 'changesApplied': changed, 'requestsReset': reset}


def restore_project(project, *, plan_only=False):
    """``restore`` for the selected install's default namespace of ``project``."""
    from memvane.journal.api import select_route
    project = Path(project).resolve(strict=True)
    decision = select_route(str(project), action='update')
    if not isinstance(decision, dict) or not decision.get('installId'):
        raise ValueError('teacher_enable_route_required')
    root = enrollment.default_namespace(project, decision['installId'])
    if not os.path.lexists(root):
        return {'status': 'no_enable_intent', 'changesApplied': 0}
    return restore(root, project, plan_only=plan_only)


def _reason(exc):
    text = str(exc)
    return text if re.fullmatch(r'[a-z0-9_]{1,80}', text) else 'teacher_enable_error'


def record(project, outcome, reason, **fields):
    """``learning.teacher`` row; diagnostics only, so a failure is ignored."""
    try:
        from memvane.journal.api import select_route
        from memvane.diagnostics.api import emit
        decision = select_route(str(project), action='update')
        emit('learning.teacher', outcome=outcome, reason=reason, decision=decision, **fields)
    except Exception:
        pass


def count_failure(root, reason, plugin_version):
    """Same-reason failure count per plugin version; False when it was not recorded."""
    with teacher_state.locked(root) as held:
        if not held:
            return False
        state = teacher_state.read_state(root)
        enable = state['enable']
        same = enable.get('reason') == reason and enable.get('pluginVersion') == plugin_version
        enable.update(failures=enable['failures'] + 1 if same else 1, reason=reason,
                      pluginVersion=plugin_version)
        return teacher_state.write_state(root, state)


def reset_failures(root):
    with teacher_state.locked(root) as held:
        if not held:
            return False
        state = teacher_state.read_state(root)
        if state['enable']['failures'] == 0:
            return True
        state['enable'].update(failures=0, reason=None, pluginVersion=None)
        return teacher_state.write_state(root, state)


def _running(project):
    """The executing host and its installed manifest version (renewal's source)."""
    from memvane.learning.context_learning.host_self import selection
    rows, _ = selection(project)
    manifest = Path(rows[0]['root']) / ('.' + rows[0]['host'] + '-plugin/plugin.json')
    return rows[0]['host'], json.loads(_recovery_read(manifest, private=False))['version']


def maintain(project, state, install_id, *, lease_fd, recover_only=False):
    """Selector maintenance step: never raises, records the outcome and failure count."""
    from memvane.kernel.memvane_version import plugin_version
    os.fstat(lease_fd)
    project = Path(project).resolve(strict=True)
    root = enrollment.default_namespace(project, install_id)
    try:
        version = plugin_version()
    except Exception:
        version = None
    if not os.path.lexists(root / setup.NAME):
        return {'status': 'not_default'}
    pending = os.path.lexists(root / INTENT)
    if recover_only and not pending:
        return {'status': 'none'}
    if not pending and teacher_state.enable_requests_stopped(teacher_state.read_state(root), version):
        return {'status': 'stopped'}
    try:
        host, host_version = _running(project)
        result = run(root, project, host=host, host_version=host_version, install_id=install_id)
    except Exception as exc:
        result = {'status': 'failed', 'reason': _reason(exc)}
    status = result.get('status')
    if status == 'enabled':
        record(project, 'enabled', None, grantedBy=GRANTED_BY)
    elif status == 'deferred':
        record(project, 'enable_deferred', result.get('reason'))
    elif status in ('failed', 'rolled_back'):
        # The count is what stops repeated requests; an unrecorded count is reported.
        counted = count_failure(root, result.get('reason') or 'teacher_enable_error', version)
        record(project, 'enable_failed', result.get('reason'), counted=counted)
    return result
