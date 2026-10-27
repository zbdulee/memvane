"""Owner-run cleanup of the retired QMD backend: daemon, LaunchAgents, trees.

Nothing here runs from a hook, a worker or a plugin update. ``plan`` is read
only. ``apply`` needs the owner's explicit approval and re-checks every fact
right before acting:

* A QMD daemon is stopped only when its pid file under the plugin backend
  directory names a live process of this user whose exact command line is
  ``<node> <plugin QMD runtime>/generations/g-<id>/qmd-native.mjs mcp --http
  --port <port of that pid file>``. The identity is read again immediately
  before ``SIGTERM``. No process is found by pattern and none is killed with
  ``SIGKILL``.
* A LaunchAgent is unloaded and removed only when ``<label>.plist`` carries
  the ``managed-by: qmd-auto-context`` marker. Backup copies are reported and
  left in place.
* A tree is removed only when no install attestation under
  ``~/.local/state/laya`` still names a file inside it (the historical
  ``profileDependencyClosure`` rows are data, not files) and no QMD daemon
  from that tree is still running.

The plugin-owned trees are the QMD runtime, the shared QMD store and the QMD
backend state. ``~/.cache/qmd`` and ``~/.config/qmd`` also hold the user's own
QMD index, configuration and model cache, so they are removed only with the
separate ``include_user_data`` approval.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time

MARKER = 'managed-by: qmd-auto-context'
LABELS = ('com.qmd-mcp-daemon', 'com.qmd-keepalive', 'com.qmd-logrotate',
          'com.qmd-index-worker')
PIDFILE = re.compile(r'daemon(?:-([0-9]{1,5}))?\.pid\Z')
STOP_WAIT_SECONDS = 10.0


class CleanupBlocked(RuntimeError):
    pass


def _account_home():
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _home(home):
    base = Path(home or Path.home())
    if not base.is_absolute():
        raise CleanupBlocked('legacy_cleanup_home_invalid')
    base = base.resolve()
    if ((os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'))
            and base == _account_home()):
        # Test and sandbox runs never inspect or change the account's real state.
        raise CleanupBlocked('legacy_cleanup_fixture_real_home')
    return base


def plugin_support(home):
    return home / 'Library' / 'Application Support' / 'qmd-auto-context'


def targets(home):
    """(path, category) of every tree the cleanup may remove."""
    support = plugin_support(home)
    return [(support / 'runtimes' / 'qmd', 'plugin_qmd_runtime'),
            (support / 'shared-qmd', 'plugin_shared_qmd_store'),
            (support / 'backend', 'plugin_qmd_backend_state'),
            (home / '.cache' / 'qmd', 'user_qmd_cache'),
            (home / '.config' / 'qmd', 'user_qmd_config')]


def _inside(path, roots):
    try:
        resolved = Path(path)
    except TypeError:
        return None
    for root in roots:
        if resolved == root or root in resolved.parents:
            return root
    return None


def _strings(value, skip=('profileDependencyClosure',)):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, skip)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in skip:
                yield from _strings(item, skip)


def attestation_references(home, roots):
    """Install attestations that still name a file inside one of ``roots``."""
    base = home / '.local' / 'state' / 'laya'
    found = []
    try:
        installs = sorted(path for path in base.iterdir() if path.is_dir())
    except FileNotFoundError:
        return found
    for install in installs:
        path = install / 'production-provider-attestation.json'
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text('utf-8'))
        except (OSError, UnicodeError, ValueError):
            found.append({'attestation': str(path), 'reason': 'attestation_unreadable'})
            continue
        named = sorted({value for value in _strings(record)
                        if value.startswith('/') and _inside(Path(value), roots)})
        if named:
            found.append({'attestation': str(path), 'paths': named[:5],
                          'count': len(named)})
    return found


def _ps(pid):
    """(uid, command line) of one pid, or None when it is not running."""
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'uid=,command='],
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=5, check=False)
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    uid, _, command = line.partition(' ')
    return int(uid), command.strip()


def _expected_command(runtime, port):
    return re.compile(r'/.+/node ' + re.escape(str(runtime)) +
                      r'/generations/g-[0-9a-f]{32}/qmd-native\.mjs mcp --http --port ' +
                      re.escape(str(port)) + r'\Z')


def daemons(home, *, ps=_ps):
    """Every pid file of the plugin's QMD daemon and what it names now."""
    backend = plugin_support(home) / 'backend'
    runtime = plugin_support(home) / 'runtimes' / 'qmd'
    rows = []
    try:
        names = sorted(path.name for path in backend.iterdir())
    except FileNotFoundError:
        return rows
    for name in names:
        match = PIDFILE.fullmatch(name)
        if match is None:
            continue
        path = backend / name
        row = {'pidFile': str(path), 'port': int(match.group(1)) if match.group(1) else None}
        try:
            raw = path.read_text('ascii').strip()
        except (OSError, UnicodeError):
            raw = ''
        if not raw.isdigit() or int(raw) <= 1:
            rows.append({**row, 'state': 'pid_file_invalid'})
            continue
        pid = int(raw)
        row['pid'] = pid
        seen = ps(pid)
        if seen is None:
            rows.append({**row, 'state': 'not_running'})
            continue
        uid, command = seen
        row['command'] = command
        if (uid != os.getuid() or row['port'] is None or
                not _expected_command(runtime, row['port']).fullmatch(command)):
            # Another process (or a reused pid) is never touched.
            rows.append({**row, 'state': 'identity_mismatch'})
            continue
        rows.append({**row, 'state': 'running_verified'})
    return rows


def launch_agents(home):
    agents = home / 'Library' / 'LaunchAgents'
    rows = []
    for label in LABELS:
        plist = agents / (label + '.plist')
        if plist.is_symlink() or not plist.exists():
            if plist.is_symlink():
                rows.append({'label': label, 'plist': str(plist), 'state': 'symlink_untouched'})
            continue
        try:
            managed = plist.is_file() and MARKER in plist.read_text('utf-8', errors='replace')
        except OSError:
            managed = False
        rows.append({'label': label, 'plist': str(plist),
                     'state': 'managed' if managed else 'unmanaged_untouched'})
    try:
        backups = sorted(path.name for path in agents.iterdir()
                         if any(path.name.startswith(label + '.plist.') for label in LABELS))
    except FileNotFoundError:
        backups = []
    for name in backups:
        rows.append({'label': name, 'plist': str(agents / name), 'state': 'backup_untouched'})
    return rows


def _tree_size(path):
    files = size = 0
    for directory, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                info = (Path(directory) / name).lstat()
            except OSError:
                continue
            files += 1
            size += info.st_size
    return files, size


def plan(home=None, *, include_user_data=False, ps=_ps, sizes=True):
    home = _home(home)
    roots = [path for path, _category in targets(home)]
    paths = []
    for path, category in targets(home):
        user = category.startswith('user_')
        row = {'path': str(path), 'category': category, 'exists': path.exists() or path.is_symlink()}
        if path.is_symlink():
            row['action'] = 'refused_symlink'
        elif not row['exists']:
            row['action'] = 'absent'
        else:
            row['action'] = 'remove' if (include_user_data or not user) else 'kept_needs_user_data_approval'
            if sizes and path.is_dir():
                row['files'], row['bytes'] = _tree_size(path)
        paths.append(row)
    found = daemons(home, ps=ps)
    agents = launch_agents(home)
    blockers = []
    references = attestation_references(home, roots)
    if references:
        blockers.append('attestation_references_legacy_path')
    for row in paths:
        if row['action'] == 'refused_symlink':
            blockers.append('legacy_cleanup_target_symlink')
    return {'schema': 'qmd-auto-context-legacy-cleanup-v1', 'home': str(home),
            'includeUserData': include_user_data, 'daemons': found,
            'launchAgents': agents, 'paths': paths,
            'attestationReferences': references,
            'blockers': sorted(set(blockers)), 'changesApplied': 0}


def _stop(row, home, *, ps, kill, sleep, clock):
    runtime = plugin_support(home) / 'runtimes' / 'qmd'
    pid, port = row['pid'], row['port']
    seen = ps(pid)
    if (seen is None or seen[0] != os.getuid() or seen[1] != row['command'] or
            not _expected_command(runtime, port).fullmatch(seen[1])):
        return 'identity_changed' if seen is not None else 'already_stopped'
    kill(pid, signal.SIGTERM)
    deadline = clock() + STOP_WAIT_SECONDS
    while clock() < deadline:
        now = ps(pid)
        if now is None or now[1] != row['command']:
            return 'stopped'
        sleep(0.2)
    return 'stop_timeout'


def _launchctl(plist):
    subprocess.run(['launchctl', 'bootout', 'gui/%d' % os.getuid(), str(plist)],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)


def _remove_tree(path, home):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or home not in path.parents):
        raise CleanupBlocked('legacy_cleanup_target_unsafe')
    shutil.rmtree(path)


def apply(home=None, *, approved=False, include_user_data=False, ps=_ps, kill=os.kill,
          launchctl=_launchctl, sleep=time.sleep, clock=time.monotonic):
    if not approved:
        raise CleanupBlocked('legacy_cleanup_approval_required')
    report = plan(home, include_user_data=include_user_data, ps=ps, sizes=False)
    if report['blockers']:
        return {**report, 'status': 'blocked'}
    home = Path(report['home'])
    changes = 0
    stopped = []
    for row in report['daemons']:
        if row['state'] != 'running_verified':
            continue
        outcome = _stop(row, home, ps=ps, kill=kill, sleep=sleep, clock=clock)
        row['stop'] = outcome
        if outcome == 'stopped':
            changes += 1
            stopped.append(row['pid'])
        elif outcome != 'already_stopped':
            report['blockers'].append('qmd_daemon_' + outcome)
    if report['blockers']:
        # A daemon that did not stop keeps its runtime and state.
        return {**report, 'status': 'blocked', 'changesApplied': changes}
    for row in report['launchAgents']:
        if row['state'] != 'managed':
            continue
        plist = Path(row['plist'])
        launchctl(plist)
        plist.unlink()
        row['state'] = 'removed'
        changes += 1
    for row in report['paths']:
        if row['action'] != 'remove':
            continue
        _remove_tree(Path(row['path']), home)
        row['action'] = 'removed'
        changes += 1
    return {**report, 'status': 'cleaned', 'stoppedPids': stopped, 'changesApplied': changes}
