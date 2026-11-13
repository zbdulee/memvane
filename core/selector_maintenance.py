"""Detached version-aware selector preparation for selected installations."""
from pathlib import Path
import os
import subprocess
import sys


def request(decision, *, force=False):
    """Start off-hook work after activation or for a newer SessionStart."""
    from laya_install_journal import inspect_journal
    from memvane_version import plugin_version, version_key
    row = inspect_journal(decision['journalPath'])
    running, highest = version_key(plugin_version()), version_key(row['maxPluginVersion'])
    if (row['state'] != 'selected' or row['installId'] != decision['installId']
            or running is None or highest is None or running < highest
            or (not force and running == highest)):
        return
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'):
        return
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER',
                                            'MEMVANE_TEST_RUNTIME_ROOT',
                                            'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT')
           if key in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                      decision['projectRoot'], 'force' if force else 'newer'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     start_new_session=True, env=env, cwd='/')


def run(project, *, force=False):
    """Serialize selector repair and monotonic version publication off-hook."""
    from contextlib import closing
    from context_learning import selector_heal
    from laya_index_host_route import select_route
    from laya_install_journal import (_open, _inspect_v1_row, inspect_journal,
                                      verify_selected_route_for_write)
    from memvane_version import plugin_version, version_key
    from sync_owner_lock import sync_owner, validate_sync_owner
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
