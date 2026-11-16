"""Detached version-aware selector preparation for selected installations."""
from pathlib import Path
from memvane.kernel.bundle_paths import entry_argv
import os
import subprocess
import sys
import time


REQUEST_SECONDS = 600
REQUEST_FILE = 'selector-heal-request.json'


def _selector_state(project):
    from memvane.learning.context_learning import learning_profile, selector_heal
    from memvane.settings import api as settings
    config = settings.find_project_config(str(project))['config']
    if config.get('sharedLearning', {}).get('profile') == learning_profile.SCHEMA:
        return learning_profile.selection_root(config, Path(project))
    return selector_heal.namespace(project)


def _missing_warm_receipt(project):
    """Only a prepared selector needs a same-version warm retry."""
    from memvane.learning.context_learning import selector_heal
    from memvane.learning.context_learning.dataset import read_private_json
    try:
        state = _selector_state(project)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if state is None:
        return False
    if not (state / selector_heal.POLICY).is_file() or not (state / 'active-checkpoint.json').is_file():
        return False
    try:
        receipt = read_private_json(state / selector_heal.WARM_MARKER)
    except (OSError, ValueError, TypeError):
        return True
    return (not isinstance(receipt, dict) or receipt.get('schema') != 'memvane-selector-warm-v1'
            or not isinstance(receipt.get('key'), str))


def _request_owned(decision, owner, *, force):
    """Apply the existing ten-minute per-install request floor under sync owner."""
    from memvane.journal.api import inspect_journal
    from memvane.journal.api import ledger_read_json as _read_json, ledger_write as _write
    from memvane.kernel.memvane_version import plugin_version, version_key
    from memvane.runtime.api import validate_sync_owner
    validate_sync_owner(owner, Path(decision['storePath']))
    row = inspect_journal(decision['journalPath'])
    running, highest = version_key(plugin_version()), version_key(row['maxPluginVersion'])
    if (row['state'] != 'selected' or row['installId'] != decision['installId']
            or running is None or highest is None or running < highest):
        return
    from memvane.learning.default_enrollment import record
    enrollment = record(decision['projectRoot'], Path(decision['journalPath']).parent,
                        decision['installId'])
    retry = running == highest and (enrollment.get('status') == 'pending_runtime'
                                    or _missing_warm_receipt(decision['projectRoot']))
    if not force and running == highest and not retry:
        return
    state = Path(decision['journalPath']).parent
    path = state / REQUEST_FILE
    now = time.time_ns()
    try:
        previous = _read_json(path)
        last = previous.get('requestedNs') if isinstance(previous, dict) else None
    except (OSError, ValueError, RuntimeError):
        last = None
    if type(last) is int and 0 <= now - last < REQUEST_SECONDS * 10**9:
        return
    _write(path, {'requestedNs': now}, replace=path.exists())
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER',
                                            'MEMVANE_TEST_RUNTIME_ROOT',
                                            'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT')
           if key in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    subprocess.Popen(entry_argv('selector-maintenance', decision['projectRoot'], 'force' if force or retry else 'newer'),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     start_new_session=True, env=env, cwd='/')


def request(decision, *, force=False, owner=None):
    """Request detached work after activation or a SessionStart, never write journal."""
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'):
        return
    if owner is not None:
        return _request_owned(decision, owner, force=force)
    from memvane.runtime.api import sync_owner
    with sync_owner(Path(decision['storePath']), wait=False) as held:
        if held is not None:
            return _request_owned(decision, held, force=force)


def run(project, *, force=False):
    """Serialize selector repair and monotonic version publication off-hook."""
    from memvane.learning.context_learning import selector_heal
    from memvane.journal.api import select_route
    from memvane.journal.api import inspect_journal, publish_plugin_version, verify_selected_route_for_write
    from memvane.kernel.memvane_version import plugin_version, version_key
    from memvane.runtime.api import sync_owner
    project = str(Path(project).resolve(strict=True))
    decision = select_route(project, action='update')
    proof = verify_selected_route_for_write(decision)
    from memvane.learning.runtime_activation import ensure_pending
    ensure_pending(decision=decision)
    lease = selector_heal._prepare_lease(None)
    try:
        row = inspect_journal(decision['journalPath'])
        running, highest = version_key(plugin_version()), version_key(row['maxPluginVersion'])
        if (running is None or highest is None or running < highest
                or (not force and running == highest)):
            return
        from memvane.learning.default_enrollment import prepare
        result = prepare(project, Path(decision['journalPath']).parent, decision['installId'], lease_fd=lease)
        if result.get('status') not in ('selected', 'preserved'):
            return
        if result.get('status') == 'preserved':
            state = _selector_state(project)
            if state is None:
                return
            result = selector_heal.heal(project, state=state, _lease_fd=lease)
            if result.get('status') in ('selector_ready', 'selector_prepared'):
                warmed = selector_heal.warm(project, state=state)
                if warmed.get('status') == 'selector_warm_busy':
                    return
        with sync_owner(Path(decision['storePath']), wait=False) as owner:
            if owner is None:
                return
            current = verify_selected_route_for_write(decision)
            if current['installId'] != proof['installId']:
                return
            publish_plugin_version(decision, owner, running)
    finally:
        os.close(lease)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[2] in ('force', 'newer'):
        try:
            run(sys.argv[1], force=sys.argv[2] == 'force')
        except Exception:
            pass
