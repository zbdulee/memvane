"""Detached version-aware selector preparation for selected installations."""
from pathlib import Path
from memvane.kernel.bundle_paths import entry_argv
import os
import subprocess
import sys
import tempfile
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
    previous = {}
    try:
        previous = _read_json(path)
        last = previous.get('requestedNs') if isinstance(previous, dict) else None
    except (OSError, ValueError, RuntimeError):
        last = None
    retry_after = previous.get('retryAfterNs', 0) if isinstance(previous, dict) else 0
    floor = last + REQUEST_SECONDS * 10**9 if type(last) is int and 0 <= now - last else 0
    if now < max(floor, retry_after if type(retry_after) is int else 0):
        return
    attempt = {'requestedNs': now, 'failures': _failure_count(previous), 'retryAfterNs': 0}
    _write(path, attempt, replace=path.exists())
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER',
                                            'MEMVANE_TEST_RUNTIME_ROOT',
                                            'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT')
           if key in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['MEMVANE_SELECTOR_REQUEST_NS'] = str(now)
    from memvane.diagnostics import api as diagnostics
    try:
        with tempfile.TemporaryFile(mode='w+b') as stderr:
            subprocess.Popen(entry_argv('selector-maintenance', decision['projectRoot'], 'force' if force or retry else 'newer'),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=stderr, close_fds=True,
                             start_new_session=True, env=env, cwd='/')
    except OSError:
        _write(path, _retry_row(attempt, now, deferred=False), replace=True)
        diagnostics.emit('learning.selector.request', outcome='failed', reason='spawn_failed', decision=decision)
        raise
    diagnostics.emit('learning.selector.request', outcome='spawned', decision=decision)


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
    try:
        result = _run(project, force=force)
        try:
            from memvane.journal.api import select_route
            from memvane.diagnostics import api as diagnostics
            decision = select_route(str(Path(project).resolve(strict=True)), action='update')
            _record_request_result(decision, completed=result is True, deferred=result is not True)
            diagnostics.emit('learning.selector.maintenance', decision=decision,
                             outcome='completed' if result is True else 'deferred',
                             reason=None if result is True else 'maintenance_incomplete')
        except Exception:
            pass
        return
    except Exception as exc:
        # Diagnostics cannot authorize work; report only after selected proof.
        try:
            from memvane.journal.api import select_route, verify_selected_route_for_write
            from memvane.diagnostics import api as diagnostics
            from memvane.kernel.resource_deferred import ResourceDeferred
            from memvane.runtime.types import ServiceFailure
            from memvane.learning.context_learning.selector_heal import HealDeferred
            decision = select_route(str(Path(project).resolve(strict=True)), action='update')
            deferred = isinstance(exc, (ResourceDeferred, HealDeferred))
            if deferred:
                _release_deferred_request(decision)
            else:
                _record_request_result(decision)
            verify_selected_route_for_write(decision)
            fixed = ('learning_prepare_memory_pressure', 'learning_prepare_busy', 'learning_pending_changed',
                     'starting', 'deadline_expired', 'learning_runtime_not_installed',
                     'learning_runtime_not_official', 'learning_runtime_changed', 'learning_profile_rebind_required',
                     'learning_prepare_lock_unsafe', 'selector_policy_invalid', 'selector_warm_lock_unsafe',
                     'adapter_not_official', 'inference_prepare_failed', 'inference_prepare_unverified')
            fixed += ('start_refused', 'start_path_invalid', 'start_io_failed', 'start_failed', 'permission_denied')
            service_codes = ('starting', 'deadline_expired', 'resource_deferred', 'queue_full',
                             'replacement_deferred', 'model_family_mismatch', 'load_failed',
                             'version_mismatch', 'request_rejected', 'request_changed', 'worker_error')
            code = exc.code if isinstance(exc, ServiceFailure) and exc.code in fixed + service_codes else str(exc)
            reason = code if code in fixed or code in service_codes else ('resource_deferred' if deferred else 'maintenance_failed')
            frames = []
            cursor = exc.__traceback__
            while cursor is not None:
                frames.append((Path(cursor.tb_frame.f_code.co_filename).name,
                               cursor.tb_frame.f_code.co_name))
                cursor = cursor.tb_next
            stage = 'prepare'
            for filename, function in reversed(frames):
                if function in ('warm', 'heal', '_prepare_namespace', '_activate_profile',
                                'publish_plugin_version', 'ensure_pending'):
                    stage = {'_prepare_namespace': 'prepare', '_activate_profile': 'profile',
                             'publish_plugin_version': 'publish', 'ensure_pending': 'proof'}.get(function, function)
                    break
            diagnostics.emit('learning.selector.maintenance', decision=decision,
                             outcome='deferred' if deferred else 'failed', reason=reason, stage=stage,
                             error=type(exc).__name__)
        except Exception:
            pass
        raise


def _failure_count(row):
    count = row.get('failures', 0) if isinstance(row, dict) else 0
    return max(0, min(32, count)) if type(count) is int else 0


def _retry_row(row, now, *, deferred):
    failures = _failure_count(row) + 1
    delay = min(3600, (60 if deferred else REQUEST_SECONDS) * 2 ** min(failures - 1, 6))
    return {'requestedNs': row['requestedNs'], 'failures': failures,
            'retryAfterNs': max(row['requestedNs'] + REQUEST_SECONDS * 10**9, now + delay * 10**9)}


def _release_deferred_request(decision):
    """Keep the request floor while recording a retryable failure."""
    _record_request_result(decision, deferred=True)


def _record_request_result(decision, *, deferred=False, completed=False):
    """Settle only the selected install's owning request, not product work proof."""
    from memvane.journal.api import inspect_journal, ledger_read_json, ledger_write
    from memvane.runtime.api import sync_owner
    from memvane.diagnostics import api as diagnostics
    value = os.environ.get('MEMVANE_SELECTOR_REQUEST_NS', '')
    if not value.isdecimal():
        return
    path = Path(decision['journalPath']).parent / REQUEST_FILE
    with sync_owner(Path(decision['storePath']), wait=True) as owner:
        if owner is None:
            diagnostics.emit('learning.selector.request', outcome='failed', reason='retry_state_busy', decision=decision)
            return
        try:
            selected = inspect_journal(decision['journalPath'])
            if selected['state'] != 'selected' or selected['installId'] != decision['installId']:
                return
            row = ledger_read_json(path)
            if isinstance(row, dict) and row.get('requestedNs') == int(value):
                if not completed and type(row.get('retryAfterNs')) is int and row['retryAfterNs'] > 0:
                    return  # The same request cannot count its failure twice.
                updated = (dict(requestedNs=int(value), failures=0, retryAfterNs=0) if completed
                           else _retry_row(row, time.time_ns(), deferred=deferred))
                ledger_write(path, updated, replace=True)
                diagnostics.emit('learning.selector.request', outcome='completed' if completed else
                                 'deferred' if deferred else 'failed', requestNs=int(value),
                                 failures=updated['failures'], retryAfterNs=updated['retryAfterNs'], decision=decision)
        except (OSError, ValueError, RuntimeError):
            diagnostics.emit('learning.selector.request', outcome='failed', reason='retry_state_failed', decision=decision)


def _run(project, *, force=False):
    """Serialize selector repair and monotonic version publication off-hook."""
    from memvane.learning.context_learning import selector_heal
    from memvane.journal.api import select_route
    from memvane.journal.api import inspect_journal, publish_plugin_version, verify_selected_route_for_write
    from memvane.kernel.memvane_version import plugin_version, version_key
    from memvane.runtime.api import sync_owner
    project = str(Path(project).resolve(strict=True))
    decision = select_route(project, action='update')
    from memvane.diagnostics import api as diagnostics
    diagnostics.emit('learning.selector.maintenance', outcome='started', decision=decision)
    try:
        proof = verify_selected_route_for_write(decision)
    except Exception:
        diagnostics.emit('learning.selector.maintenance', outcome='failed',
                         reason='selected_proof_unavailable', decision=decision)
        raise
    from memvane.learning.runtime_activation import ensure_pending
    from memvane.kernel.resource_deferred import ResourceDeferred
    try:
        ensure_pending(decision=decision)
    except ResourceDeferred as exc:
        diagnostics.emit('learning.selector.maintenance', outcome='deferred',
                         reason='proof_deferred', decision=decision)
    lease = selector_heal._prepare_lease(None)
    try:
        row = inspect_journal(decision['journalPath'])
        running, highest = version_key(plugin_version()), version_key(row['maxPluginVersion'])
        if running is None or highest is None:
            return False
        if running < highest or (not force and running == highest):
            return True
        from memvane.learning.default_enrollment import prepare
        result = prepare(project, Path(decision['journalPath']).parent, decision['installId'], lease_fd=lease)
        if result.get('status') == 'selected':
            from memvane.diagnostics import api as diagnostics
            diagnostics.emit('learning.selector.warm', outcome=result.get('warm'), decision=decision)
        if result.get('status') not in ('selected', 'preserved'):
            return False
        if result.get('status') == 'preserved':
            state = _selector_state(project)
            if state is None:
                return False
            result = selector_heal.heal(project, state=state, _lease_fd=lease)
            if result.get('status') not in ('selector_ready', 'selector_prepared'):
                return False
            warmed = selector_heal.warm(project, state=state)
            if warmed.get('status') not in ('selector_warm', 'selector_warm_current'):
                return False
            from memvane.diagnostics import api as diagnostics
            diagnostics.emit('learning.selector.warm', outcome=warmed.get('status'), decision=decision)
        with sync_owner(Path(decision['storePath']), wait=False) as owner:
            if owner is None:
                return False
            current = verify_selected_route_for_write(decision)
            if current['installId'] != proof['installId']:
                return False
            publish_plugin_version(decision, owner, running)
            return True
    finally:
        os.close(lease)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[2] in ('force', 'newer'):
        try:
            run(sys.argv[1], force=sys.argv[2] == 'force')
        except Exception:
            pass
