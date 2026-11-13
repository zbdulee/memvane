"""Detached version-aware selector preparation for selected installations."""
from pathlib import Path
from memvane.kernel.bundle_paths import entry_argv
import os
import subprocess
import sys
import time


REQUEST_SECONDS = 600
REQUEST_FILE = 'selector-heal-request.json'


def _missing_warm_receipt(project):
    """Only a prepared selector needs a same-version warm retry."""
    from memvane.learning.context_learning import selector_heal
    from memvane.learning.context_learning.dataset import read_private_json
    state = selector_heal.namespace(project)
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
    from memvane.journal.install_journal import inspect_journal
    from memvane.install.ledger_common import _read_json, _write
    from memvane.kernel.memvane_version import plugin_version, version_key
    from memvane.sync.api import validate_sync_owner
    validate_sync_owner(owner, Path(decision['storePath']))
    row = inspect_journal(decision['journalPath'])
    running, highest = version_key(plugin_version()), version_key(row['maxPluginVersion'])
    if (row['state'] != 'selected' or row['installId'] != decision['installId']
            or running is None or highest is None or running < highest):
        return
    retry = running == highest and _missing_warm_receipt(decision['projectRoot'])
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
    from memvane.sync.api import sync_owner
    with sync_owner(Path(decision['storePath']), wait=False) as held:
        if held is not None:
            return _request_owned(decision, held, force=force)


def run(project, *, force=False):
    """Serialize selector repair and monotonic version publication off-hook."""
    from contextlib import closing
    from memvane.learning.context_learning import selector_heal
    from memvane.route.index_host_route import select_route
    from memvane.journal.install_journal import (_open, _inspect_v1_row, inspect_journal,
                                      verify_selected_route_for_write)
    from memvane.kernel.memvane_version import plugin_version, version_key
    from memvane.sync.api import sync_owner, validate_sync_owner
    project = str(Path(project).resolve(strict=True))
    decision = select_route(project, action='update')
    proof = verify_selected_route_for_write(decision)
    lease = selector_heal._prepare_lease(None)
    try:
        row = inspect_journal(decision['journalPath'])
        running, highest = version_key(plugin_version()), version_key(row['maxPluginVersion'])
        if (running is None or highest is None or running < highest
                or (not force and running == highest)):
            return
        result = selector_heal.heal(project, _lease_fd=lease)
        if result.get('status') in ('selector_ready', 'selector_prepared'):
            warmed = selector_heal.warm(project)
            if warmed.get('status') == 'selector_warm_busy':
                return
        with sync_owner(Path(decision['storePath']), wait=False) as owner:
            if owner is None:
                return
            current = verify_selected_route_for_write(decision)
            if current['installId'] != proof['installId']:
                return
            path, db = _open(decision['journalPath'], write=True)
            with closing(db):
                db.execute('BEGIN IMMEDIATE')
                try:
                    row = _inspect_v1_row(db)
                    old = version_key(row['maxPluginVersion'])
                    if row['state'] != 'selected' or old is None:
                        raise ValueError('selector_version_changed')
                    if running > old:
                        validate_sync_owner(owner, Path(decision['storePath']))
                        if db.execute("UPDATE selection SET max_plugin_version=?,epoch=epoch+1 "
                                      "WHERE id=1 AND state='selected' AND epoch=?",
                                      (plugin_version(), row['epoch'])).rowcount != 1:
                            raise ValueError('selector_version_changed')
                        _inspect_v1_row(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
    finally:
        os.close(lease)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[2] in ('force', 'newer'):
        try:
            run(sys.argv[1], force=sys.argv[2] == 'force')
        except Exception:
            pass
