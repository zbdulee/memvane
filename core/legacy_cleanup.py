"""Owner-run cleanup of the retired QMD backend: daemon, LaunchAgents, trees.

Nothing here runs from a hook, a worker or a plugin update. ``plan`` is read
only. ``apply`` needs the owner's explicit approval and re-checks every fact
right before acting:

* A QMD daemon is stopped only when its pid file under the plugin backend
  directory names a live process of this user whose exact command line is
  ``<node> <plugin QMD runtime>/generations/g-<id>/qmd-native.mjs mcp --http
  --port <port of that pid file>`` (the port-less legacy ``daemon.pid`` takes
  the port its process carries). The identity is read again immediately
  before ``SIGTERM``. No process is killed by pattern and none with
  ``SIGKILL``. A live process that names a plugin target but fails that
  identity (a node process whose executable or script lies inside a target),
  found through a pid file or the read-only process table, blocks
  every change (``qmd_daemon_unverified``). Without a pidfd (macOS) a pid can
  still be reused between the last identity read and the signal; the checks
  target accidents, not another process of the same user.
* A LaunchAgent is unloaded and removed only when ``<label>.plist`` carries
  the ``managed-by: qmd-auto-context`` marker the retired backend wrote. Backup copies are reported and
  left in place.
* A tree is removed only when no install attestation under
  ``~/.local/state/memvane`` (or the pre-rename ``~/.local/state/laya``) still names a file inside it (the historical
  ``profileDependencyClosure`` rows are data, not files), no install route is
  still bound to a bundle that ships the QMD backend (it would recreate the
  QMD directories), and no QMD daemon from that tree is still running. Every
  directory from HOME down to the target must be a real directory of this
  user, and the delete walks directory descriptors without following links
  (``legacy_cleanup_target_unsafe``).

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
import signal
import stat
import subprocess
import time

import safe_rmtree

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
    """The pre-rename support directory that held the retired QMD backend."""
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


def _install_dirs(home):
    """Install state roots under the MemVane and the pre-rename state base."""
    import memvane_paths
    installs = []
    for base in (memvane_paths.state_base(home), memvane_paths.legacy_state_base(home)):
        try:
            installs.extend(sorted(path for path in base.iterdir() if path.is_dir()))
        except FileNotFoundError:
            continue
    return installs


def attestation_references(home, roots):
    """Install attestations that still name a file inside one of ``roots``."""
    found = []
    installs = _install_dirs(home)
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


def older_bundles(home):
    """Installs still bound to a bundle that ships the QMD backend.

    Such a bundle recreates ``~/.cache/qmd`` and ``~/.config/qmd`` on its next
    hook, so cleanup waits until every install moved to a bundle without it.
    """
    found = []
    installs = _install_dirs(home)
    for install in installs:
        route = install / 'reviewed-route.json'
        if not route.is_file():
            continue
        try:
            root = json.loads(route.read_text('utf-8')).get('pluginRoot')
        except (OSError, UnicodeError, ValueError, AttributeError):
            found.append({'route': str(route), 'reason': 'route_unreadable'})
            continue
        if isinstance(root, str) and (Path(root) / 'core' / 'backend_manager.sh').exists():
            found.append({'route': str(route), 'pluginRoot': root})
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
    # ``port=None`` is the port-less legacy pid file: any port, captured.
    return re.compile(r'/.+/node ' + re.escape(str(runtime)) +
                      r'/generations/g-[0-9a-f]{32}/qmd-native\.mjs mcp --http --port ' +
                      (r'([0-9]{1,5})' if port is None else re.escape(str(port))) + r'\Z')


def _plugin_targets(home):
    return [path for path, category in targets(home) if category.startswith('plugin_')]


# ``<absolute executable ending in /node>[ <argv>]``. The executable may hold a
# space (``Application Support``) but never `` /`` or `` -``, which start the
# next argument.
_NODE_ARGV = re.compile(r'(/(?:[^ ]| (?![/-]))*?/node)(?: (.*))?\Z', re.S)


def _within(path, root):
    return path == root or path.startswith(root + '/')


def _uses_target(command, home):
    """A node process whose executable or script path lies inside a plugin target.

    Structural, not a substring match: an editor, a backup copy
    (``backend-backup``) or a script that only mentions a target never counts.
    """
    match = _NODE_ARGV.match(command)
    if match is None:
        return False
    exe, rest = match.group(1), match.group(2) or ''
    while rest.startswith('-'):
        # Node options before the script carry no space.
        rest = rest.partition(' ')[2]
    for target in map(str, _plugin_targets(home)):
        if (_within(exe, target) or rest == target or
                rest.startswith(target + '/') or rest.startswith(target + ' ')):
            return True
    return False


def _ps_all():
    """(pid, uid, command line) of every process; read only."""
    result = subprocess.run(['ps', '-axww', '-o', 'pid=,uid=,command='],
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise CleanupBlocked('legacy_cleanup_process_table_unavailable')
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
    return rows


def unverified_processes(home, verified=(), *, ps_all=_ps_all):
    """Live node processes running from a plugin target, not covered by a verified pid file."""
    return [{'pid': pid, 'uid': uid, 'command': command}
            for pid, uid, command in ps_all()
            if pid != os.getpid() and pid not in verified and _uses_target(command, home)]


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
        match = _expected_command(runtime, row['port']).fullmatch(command)
        if uid != os.getuid() or match is None:
            # Another process (or a reused pid) is never touched, but one that
            # still names a plugin tree keeps every tree in place.
            state = 'running_unverified' if _uses_target(command, home) else 'identity_mismatch'
            rows.append({**row, 'state': state})
            continue
        if row['port'] is None:
            # The legacy port-less pid file: pin the port the process carries
            # so the re-check before SIGTERM compares the same exact identity.
            row['port'] = int(match.group(1))
            row['legacy'] = True
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


def _ancestry_safe(path, home):
    """False when a directory between HOME and ``path`` is a link or not ours."""
    cursor = home
    for name in path.relative_to(home).parts[:-1]:
        cursor = cursor / name
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            return True
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
                info.st_uid != os.getuid()):
            return False
    return True


def plan(home=None, *, include_user_data=False, ps=_ps, ps_all=_ps_all, sizes=True):
    home = _home(home)
    roots = [path for path, _category in targets(home)]
    paths = []
    for path, category in targets(home):
        user = category.startswith('user_')
        row = {'path': str(path), 'category': category, 'exists': path.exists() or path.is_symlink()}
        if user and not include_user_data:
            # Outside the approved scope: never inspected for removal, never a
            # blocker (a user-managed ~/.config link stays as it is).
            row['action'] = 'preserved'
            row['reason'] = 'needs_user_data_approval'
        elif not _ancestry_safe(path, home):
            row['action'] = 'refused_unsafe_path'
        elif path.is_symlink():
            row['action'] = 'refused_symlink'
        elif not row['exists']:
            row['action'] = 'absent'
        else:
            row['action'] = 'remove'
            if sizes and path.is_dir():
                row['files'], row['bytes'] = _tree_size(path)
        paths.append(row)
    found = daemons(home, ps=ps)
    verified = {row['pid'] for row in found if row['state'] == 'running_verified'}
    others = unverified_processes(home, verified, ps_all=ps_all)
    agents = launch_agents(home)
    blockers = []
    if others or any(row['state'] == 'running_unverified' for row in found):
        blockers.append('qmd_daemon_unverified')
    references = attestation_references(home, roots)
    if references:
        blockers.append('attestation_references_legacy_path')
    older = older_bundles(home)
    if older:
        blockers.append('older_host_bundle_bound')
    for row in paths:
        if row['action'] == 'refused_symlink':
            blockers.append('legacy_cleanup_target_symlink')
        elif row['action'] == 'refused_unsafe_path':
            blockers.append('legacy_cleanup_target_unsafe')
    return {'schema': 'qmd-auto-context-legacy-cleanup-v1', 'home': str(home),
            'includeUserData': include_user_data, 'daemons': found,
            'unverifiedProcesses': others,
            'launchAgents': agents, 'paths': paths,
            'attestationReferences': references, 'olderBundles': older,
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
    """Delete ``path`` through descriptors anchored at the verified HOME.

    Every hop from HOME down is a real directory of this user opened with
    ``O_NOFOLLOW`` relative to its parent, so a link anywhere on the way, or
    swapped in after the plan, refuses instead of redirecting the delete.
    """
    try:
        parts = path.relative_to(home).parts
    except ValueError:
        raise CleanupBlocked('legacy_cleanup_target_unsafe') from None
    if not parts:
        raise CleanupBlocked('legacy_cleanup_target_unsafe')
    uid = os.getuid()
    try:
        root = safe_rmtree.open_root(home, uid=uid)
        try:
            parent = safe_rmtree.open_path(root, parts[:-1], uid=uid)
        finally:
            os.close(root)
        try:
            safe_rmtree.rmtree_at(parent, parts[-1], uid=uid, path=path)
        finally:
            os.close(parent)
    except safe_rmtree.UnsafeTree as exc:
        if str(exc) == 'safe_rmtree_mount_point':
            raise CleanupBlocked('legacy_cleanup_target_mount_point') from None
        raise CleanupBlocked('legacy_cleanup_target_unsafe') from None


def apply(home=None, *, approved=False, include_user_data=False, ps=_ps, ps_all=_ps_all,
          kill=os.kill, launchctl=_launchctl, sleep=time.sleep, clock=time.monotonic):
    if not approved:
        raise CleanupBlocked('legacy_cleanup_approval_required')
    report = plan(home, include_user_data=include_user_data, ps=ps, ps_all=ps_all,
                  sizes=False)
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
    if not report['blockers']:
        # Nothing may still run from a tree about to be removed.
        left = unverified_processes(home, ps_all=ps_all)
        if left:
            report['unverifiedProcesses'] = left
            report['blockers'].append('qmd_daemon_unverified')
    if report['blockers']:
        # A daemon that did not stop keeps its runtime and state.
        return {**report, 'status': 'blocked', 'changesApplied': changes}
    completed, failed = [], []
    for row in report['launchAgents']:
        if row['state'] != 'managed':
            continue
        plist = Path(row['plist'])
        try:
            launchctl(plist)
            plist.unlink()
        except OSError as exc:
            row['state'], row['reason'] = 'failed', _failure(exc)
            failed.append({'plist': row['plist'], 'reason': row['reason']})
            continue
        row['state'] = 'removed'
        completed.append({'plist': row['plist']})
        changes += 1
    for row in report['paths']:
        if row['action'] != 'remove':
            continue
        try:
            _remove_tree(Path(row['path']), home)
        except (CleanupBlocked, OSError) as exc:
            # Report and keep going: what was removed stays reported.
            row['action'], row['reason'] = 'failed', _failure(exc)
            failed.append({'path': row['path'], 'reason': row['reason']})
            continue
        row['action'] = 'removed'
        completed.append({'path': row['path']})
        changes += 1
    return {**report, 'status': 'partial' if failed else 'cleaned', 'partial': bool(failed),
            'completed': completed, 'failed': failed, 'stoppedPids': stopped,
            'changesApplied': changes}


def _failure(exc):
    if isinstance(exc, CleanupBlocked):
        return str(exc)
    import errno
    return 'legacy_cleanup_remove_failed:' + errno.errorcode.get(exc.errno or 0, 'unknown')
