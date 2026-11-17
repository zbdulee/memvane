"""CAS publication/recovery of default enrollment bindings, with a consumer fence."""
import base64
import fcntl
import json
import os
from pathlib import Path

from memvane.learning import default_renewal_plan as planning
from memvane.learning import default_enrollment as enrollment, project_learning_setup as setup
from memvane.learning.context_learning import learning_profile as profile
from memvane.learning.context_learning.dataset import read_private_json
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.context_learning.training_cursor import _sync
from memvane.learning.project_learning_recovery import _recovery_read, _restore_bytes
from memvane.kernel.memvane_version import version_key
from memvane.runtime.api import coordination_lock
from memvane.settings.api import settings_lock

SCHEMA = 'memvane-default-renewal-v1'


def _read_targets(root):
    return {name: _recovery_read(root/name) for name in planning.TARGETS}


def _decode(raws):
    return {name: json.loads(raw) for name, raw in raws.items()}


def _pack(raws):
    return {name: {'sha256': planning.sha(raw), 'bytes': base64.b64encode(raw).decode('ascii')}
            for name, raw in raws.items()}


def _unpack(value):
    if not isinstance(value, dict) or set(value) != set(planning.TARGETS):
        raise ValueError('renewal_intent_invalid')
    result = {}
    for name, row in value.items():
        if not isinstance(row, dict) or set(row) != {'sha256', 'bytes'}:
            raise ValueError('renewal_intent_invalid')
        raw = base64.b64decode(row['bytes'], validate=True)
        if planning.sha(raw) != row['sha256']:
            raise ValueError('renewal_intent_invalid')
        result[name] = raw
    return result


def _chain(project, root, install_id, intent):
    if (not isinstance(intent, dict) or set(intent) != {
            'schema', 'phase', 'projectRoot', 'namespaceRoot', 'installId', 'host', 'hostVersion',
            'settingsSha256', 'setupSha256', 'old', 'new'}
            or intent['schema'] != SCHEMA or intent['phase'] not in ('intent', 'rolling_back')
            or intent['projectRoot'] != str(project) or intent['namespaceRoot'] != str(root)
            or intent['installId'] != install_id or intent['host'] not in ('claude', 'codex')
            or version_key(intent['hostVersion']) is None):
        raise ValueError('renewal_intent_invalid')
    if (file_sha(setup._project_settings(project)) != intent['settingsSha256']
            or file_sha(root/setup.NAME) != intent['setupSha256']
            or not enrollment.authorized(project, root)):
        raise ValueError('renewal_inputs_changed')
    old, new = _unpack(intent['old']), _unpack(intent['new'])
    current = _read_targets(root)
    if any(current[name] not in (old[name], new[name]) for name in planning.TARGETS):
        raise ValueError('renewal_mixed_state')
    from memvane.learning.context_learning.host_self import selection
    rows, _ = selection(project)
    manifest = Path(rows[0]['root'])/('.'+rows[0]['host']+'-plugin/plugin.json')
    # Installed manifests are public-readable; retain owner, link, write-bit
    # and fd/path stability checks without imposing private-state permissions.
    running = version_key(json.loads(_recovery_read(manifest, private=False))['version'])
    if running is None or running < version_key(intent['hostVersion']):
        raise ValueError('renewal_owner_newer')
    if running == version_key(intent['hostVersion']) and rows[0]['host'] != intent['host']:
        raise ValueError('renewal_owner_newer')
    return old, new


def _drop(root):
    (root/profile.RENEWAL).unlink()
    _sync(root)


def _restore(project, root, install_id, intent):
    old, new = _chain(project, root, install_id, intent)
    intent = {**intent, 'phase': 'rolling_back'}
    setup._write(root/profile.RENEWAL, intent)
    for name in reversed(planning.TARGETS):
        _chain(project, root, install_id, intent)
        current = _recovery_read(root/name)
        if current != old[name]:
            _restore_bytes(root/name, old[name], expected_sha256=planning.sha(current))
    _chain(project, root, install_id, intent)
    _drop(root)


def _publish(project, root, install_id, intent):
    old, new = _chain(project, root, install_id, intent)
    for name in planning.TARGETS:
        _chain(project, root, install_id, intent)
        current = _recovery_read(root/name)
        if current != new[name]:
            # Every write has a real CAS; a conflict keeps the consumer fence.
            _restore_bytes(root/name, new[name], expected_sha256=planning.sha(current))
    _chain(project, root, install_id, intent)
    if _read_targets(root) != new:
        raise ValueError('renewal_mixed_state')
    # Re-prove source, settings, runtime/model, expiry and all authority fields
    # against the original snapshot before admitting any consumer.
    target = planning.plan(project, root, _decode(old))
    if target is None or {name: planning.raw(value) for name, value in target.items()} != new:
        raise ValueError('renewal_target_changed')
    _drop(root)


def renew(project, state, install_id, *, lease_fd):
    """Caller holds the existing prepare lease; no settings or setup writes."""
    os.fstat(lease_fd)
    project = Path(project).resolve(strict=True)
    root = enrollment.default_namespace(project, install_id)
    if not profile.present(root):
        return {'status': 'not_default'}
    root = setup._path(root, private=True, directory=True)
    with setup._locked(root), settings_lock(project):
        fd = coordination_lock(root/'learning-profile.lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = root/profile.RENEWAL
            if os.path.lexists(path):
                intent = read_private_json(setup._path(path, private=True))
                old, new = _chain(project, root, install_id, intent)
                if intent['phase'] == 'rolling_back':
                    _restore(project, root, install_id, intent)
                else:
                    try:
                        target = planning.plan(project, root, _decode(old))
                    except ValueError:
                        # A revoked/expired or changed policy/runtime never
                        # completes a new approval. Restore only the CAS pair.
                        _restore(project, root, install_id, intent)
                        raise
                    if target is not None:
                        intended = _decode(new)[planning.scope.NAME]['proposal']['binding']
                        proposed = target[planning.scope.NAME]['proposal']['binding']
                        installed = proposed['installations'][0]
                        if (installed['version'] == intent['hostVersion']
                                and (installed != intended['installations'][0]
                                     or proposed['hostRegistry'] != intended['hostRegistry'])):
                            raise ValueError('renewal_same_version_changed')
                    if target is not None and {name: planning.raw(value) for name, value in target.items()} == new:
                        _publish(project, root, install_id, intent)
                        return {'status': 'renewed'}
                    _restore(project, root, install_id, intent)
            old = _read_targets(root)
            target = planning.plan(project, root, _decode(old))
            if target is None:
                return {'status': 'current'}
            new = {name: planning.raw(value) for name, value in target.items()}
            from memvane.learning.context_learning.host_self import selection
            rows, _ = selection(project)
            installed = target[planning.scope.NAME]['proposal']['binding']['installations'][0]
            intent = {'schema': SCHEMA, 'phase': 'intent', 'projectRoot': str(project),
                      'namespaceRoot': str(root), 'installId': install_id, 'host': rows[0]['host'],
                      'hostVersion': installed['version'], 'settingsSha256': file_sha(setup._project_settings(project)),
                      'setupSha256': file_sha(root/setup.NAME), 'old': _pack(old), 'new': _pack(new)}
            setup._write(path, intent)
            _publish(project, root, install_id, intent)
            return {'status': 'renewed'}
        finally:
            os.close(fd)
