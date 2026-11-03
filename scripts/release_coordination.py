"""Cooperative developer release coordination; never an owner-proof bypass.

The operator owns installation consent and discovers existing state roots.
Actual upgrades still run the installed bundle's ordinary owner checks. Locks
remain on disk, and only this helper's descriptors/processes are cleaned up.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time

LOCK = 'bundle-auto-upgrade.lock'
MAX_STATES = 128
MAX_WIRE_BYTES = 1024 * 1024
COMPLETE = frozenset(('current', 'active_upgrade_complete', 'companion_sealed',
                      'bound_upgrade_complete', 'inactive_upgrade_complete',
                      'state_move_complete', 'project_dir_move_complete'))


class ReleaseError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def collect_registered_bridge_preflight(state, *, release_commit, expected_hosts):
    """Collect registered v4 host evidence using the installer's selected core."""
    from laya_bridge_readiness import (begin_bridge_preflight, collect_bridge_preflight,
                                       check_bridge_preflight)
    challenge = begin_bridge_preflight(state, release_commit=release_commit,
                                       expected_hosts=expected_hosts)
    refs = [collect_bridge_preflight(challenge, host=host) for host in sorted(expected_hosts)]
    return check_bridge_preflight(refs, release_commit=release_commit, expected_hosts=expected_hosts)


def _completed_rows(results):
    """Every reported phase must be a known terminal outcome, not just a row."""
    if not isinstance(results, list) or not results or len(results) > MAX_STATES * 2:
        raise ReleaseError('upgrade_incomplete')
    rows = []
    learning = {'learningState': {'learning_state_moved'},
                'selector': {'selector_not_prepared', 'selector_ready', 'selector_prepared'},
                'selectorWarm': {'selector_warm_current', 'selector_warm'}}
    for row in results:
        if (not isinstance(row, dict) or not isinstance(row.get('stateRoot'), str)
                or not Path(row['stateRoot']).is_absolute()):
            raise ReleaseError('worker_result_invalid')
        if 'status' not in row:
            phases = set(row) - {'stateRoot'}
            if not phases or not phases <= set(learning) or any(
                    not isinstance(row[key], str) or row[key] not in learning[key] for key in phases):
                raise ReleaseError('upgrade_incomplete')
            continue
        if (set(row) - {'stateRoot', 'status', 'reason', 'kind', 'file', 'stateMove', 'projectDirMove'}
                or not isinstance(row.get('status'), str) or row['status'] not in COMPLETE
                or not isinstance(row.get('kind'), str) or row['kind'] not in ('active', 'bound', 'inactive')
                or ('reason' in row and not isinstance(row['reason'], str))):
            raise ReleaseError('upgrade_incomplete')
        required = {'active_upgrade_complete': 'active', 'bound_upgrade_complete': 'bound',
                    'inactive_upgrade_complete': 'inactive', 'project_dir_move_complete': 'active'}
        if row['status'] in required and row['kind'] != required[row['status']]:
            raise ReleaseError('upgrade_incomplete')
        for key, allowed in (('stateMove', {'state_move_complete', 'state_move_current'}),
                             ('projectDirMove', {'project_dir_move_complete', 'project_dir_current'})):
            if key not in row:
                continue
            phase = row[key]
            if (not isinstance(phase, dict) or not isinstance(phase.get('status'), str)
                    or phase['status'] not in allowed
                    or ('stateRoot' in phase and phase['stateRoot'] != row['stateRoot'])):
                raise ReleaseError('upgrade_incomplete')
        rows.append(row)
    states = [row['stateRoot'] for row in rows]
    if not rows or len(states) != len(set(states)):
        raise ReleaseError('worker_result_invalid')
    return rows


def _preferred_owner(upgrade, state, project, root, expected_kind):
    """Stable validated journal/build metadata; no model or repeated bundle hash."""
    from laya_install_journal import inspect_journal
    import laya_initial_build as build
    state = Path(state)
    _directory(state)
    path = state / 'install-journal.sqlite'
    journal = inspect_journal(path)
    record = build.read(state)
    kind, classified = upgrade.classify(state)
    if (kind != expected_kind or kind not in ('active', 'bound', 'inactive')
            or not isinstance(journal, dict) or type(journal.get('epoch')) is not int
            or journal['epoch'] < 1 or journal.get('projectRoot') != project
            or journal.get('journalPath') != str(path)
            or not isinstance(journal.get('scopeId'), str)
            or not re.fullmatch('[0-9a-f]{32}', journal['scopeId'])
            or type(journal.get('newWriteSeen')) is not bool
            or (record is not None and not isinstance(record, dict))):
        raise ReleaseError('preferred_owner_not_selected')
    if record is not None and (type(record.get('epoch')) is not int or record['epoch'] < 1
            or not isinstance(record.get('phase'), str)
            or record['phase'] not in ('registered', 'building', 'sealed', 'blocked', 'bound')):
        raise ReleaseError('preferred_owner_not_selected')
    if kind == 'active':
        if journal.get('state') not in ('active', 'restored_old') or journal.get('pending') is not None:
            raise ReleaseError('preferred_owner_not_selected')
        identity = journal.get('selected')
        for key in ('selectedRouteSha256', 'providerAttestationSha256', 'providerFileIdentitySha256'):
            if not isinstance(journal.get(key), str) or not re.fullmatch('[0-9a-f]{64}', journal[key]):
                raise ReleaseError('preferred_owner_not_selected')
    else:
        if (journal.get('state') != 'candidate_ready' or journal.get('selected') is not None
                or journal.get('previous') is not None or journal.get('newWriteSeen') is not False
                or not isinstance(record, dict) or type(record.get('epoch')) is not int
                or record['epoch'] < 1 or record.get('phase') not in (
                    ('bound',) if kind == 'bound' else ('registered', 'building', 'sealed', 'blocked'))):
            raise ReleaseError('preferred_owner_not_selected')
        identity = journal.get('pending')
    if (not isinstance(identity, dict) or classified != identity
            or identity.get('pluginRoot') != root or identity.get('bundleKind') != 'full_plugin'
            or not isinstance(identity.get('vectorProfile'), dict)):
        raise ReleaseError('preferred_owner_not_selected')
    for key, pattern in (('installId', '[0-9a-f]{32}'), ('generation', 'g-[0-9a-f]{32}'),
                         ('sourceCommit', '[0-9a-f]{40}|[0-9a-f]{64}'),
                         ('bundleManifestSha256', '[0-9a-f]{64}'),
                         ('ownershipManifestSha256', '[0-9a-f]{64}')):
        if not isinstance(identity.get(key), str) or not re.fullmatch(pattern, identity[key]):
            raise ReleaseError('preferred_owner_not_selected')
    profile = identity['vectorProfile'].get('profileId')
    if not isinstance(profile, str) or not re.fullmatch('[0-9a-f]{64}', profile):
        raise ReleaseError('preferred_owner_not_selected')
    active_route = None
    if kind == 'active' and record is not None:
        from laya_index_host_route import select_route
        from laya_index_runtime import capacity_store_paths
        active_route = select_route(project, action='recall')
        if (active_route.get('state') != 'candidate'
                or any(active_route.get(key) != identity.get(key) for key in
                       ('pluginRoot', 'installId', 'generation', 'vectorProfile'))
                or active_route.get('scopeId') != journal['scopeId']):
            raise ReleaseError('preferred_owner_not_selected')
        catalog, bindings = capacity_store_paths(active_route['dbPath'])
        stable = {'projectRoot': project, 'stateRoot': str(state), 'journalPath': str(path),
                  'installId': identity['installId'], 'scopeId': journal['scopeId'],
                  'dbIncarnation': active_route['dbIncarnation'],
                  'candidateDb': active_route['dbPath'], 'queueDb': active_route['queuePath'],
                  'catalogDb': str(catalog), 'bindingStore': str(bindings),
                  'routeFile': str(state / 'reviewed-route.json'), 'vectorProfileId': profile,
                  'collections': active_route['collections']}
        if (not isinstance(record.get('identity'), dict)
                or any(record['identity'].get(key) != value for key, value in stable.items())):
            raise ReleaseError('preferred_owner_not_selected')
    if kind != 'active':
        expected = {'stateRoot': str(state), 'projectRoot': project, 'journalPath': str(path),
                    'routeFile': str(state / 'reviewed-route.json'),
                    'installId': identity['installId'], 'scopeId': journal.get('scopeId'),
                    'vectorProfileId': profile, 'routeSha256': journal.get('pendingRouteSha256')}
        if (not isinstance(record.get('identity'), dict)
                or any(record['identity'].get(key) != value for key, value in expected.items())
                or not isinstance(expected['routeSha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', expected['routeSha256'])):
            raise ReleaseError('preferred_owner_not_selected')
    # Inspectors validate their own on-disk schemas. Compare complete snapshots
    # again so an epoch/phase/owner switch cannot be combined into one receipt.
    if (inspect_journal(path) != journal or build.read(state) != record
            or upgrade.classify(state) != (kind, identity)
            or (active_route is not None and select_route(project, action='recall') != active_route)):
        raise ReleaseError('release_state_changed')
    return {'stateRoot': str(state), 'pluginRoot': root}


def _node(info):
    return info.st_dev, info.st_ino


def _directory(path):
    path = Path(path)
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ReleaseError('release_state_unsafe')
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ReleaseError('release_state_unsafe')
        return _node(info)
    except (OSError, ValueError, UnicodeError):
        raise ReleaseError('release_state_unsafe') from None


class _Lease:
    def __init__(self, state):
        self.state = Path(state)
        self.directory = self.fd = None
        try:
            self.root_node = _directory(self.state)
            self.directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if _node(os.fstat(self.directory)) != self.root_node:
                raise ReleaseError('release_state_changed')
            self.fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                              0o600, dir_fd=self.directory)
            os.set_inheritable(self.directory, False)
            os.set_inheritable(self.fd, False)
            self.file_node = _node(os.fstat(self.fd))
            self.check()
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseError('release_busy') from None
            self.check()
        except OSError:
            self.close()
            raise ReleaseError('release_lock_unsafe') from None
        except BaseException:
            self.close()
            raise

    def check(self):
        try:
            info = os.fstat(self.fd)
            named = os.stat(LOCK, dir_fd=self.directory, follow_symlinks=False)
            if (_directory(self.state) != self.root_node
                    or _node(os.fstat(self.directory)) != self.root_node
                    or _node(info) != self.file_node or _node(named) != self.file_node
                    or not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(named.st_mode)
                    or info.st_uid != os.getuid() or named.st_uid != os.getuid()
                    or info.st_nlink != 1 or named.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or stat.S_IMODE(named.st_mode) != 0o600):
                raise ReleaseError('release_lock_changed')
        except OSError:
            raise ReleaseError('release_lock_unsafe') from None

    def close(self):
        for field in ('fd', 'directory'):
            fd = getattr(self, field)
            setattr(self, field, None)
            if fd is not None:
                # Never unlink or close a descriptor a callback replaced.
                expected = getattr(self, 'file_node' if field == 'fd' else 'root_node', None)
                try:
                    if expected is None or _node(os.fstat(fd)) == expected:
                        os.close(fd)
                except OSError:
                    pass


def coordinate_release(states, *, install_both, prepare_worker):
    """Hold every bundle lock through installation, then gate one worker each.

    Callbacks are trusted operator code, not receipts or authorization claims.
    They must use bounded installer calls and validate both installed bundles.
    A post-unlock race remains possible; PreparedWorker checks the actual owner.
    """
    if type(states) is not dict or not states or len(states) > 32:
        raise ReleaseError('release_plan_invalid')
    seen, groups = set(), {}
    for project, roots in states.items():
        if (not isinstance(project, str) or not project or not Path(project).is_absolute()
                or type(roots) not in (list, tuple) or not roots):
            raise ReleaseError('release_plan_invalid')
        groups[project] = []
        for root in roots:
            if not isinstance(root, (str, Path)):
                raise ReleaseError('release_plan_invalid')
            root = Path(root)
            if root in seen or len(seen) >= MAX_STATES:
                raise ReleaseError('release_plan_invalid')
            seen.add(root)
            groups[project].append(root)
    leases, result = {}, []
    try:
        for root in sorted(seen):
            leases[root] = _Lease(root)
        install_both()
        for lease in leases.values():
            lease.check()
        for project, roots in groups.items():
            worker = prepare_worker(project)
            try:
                worker.ready()
                for lease in leases.values():
                    lease.check()
                for root in roots:
                    leases.pop(root).close()
                worker.go()
                result.append(worker.finish())
            finally:
                worker.close()
        return result
    finally:
        for lease in reversed(list(leases.values())):
            lease.close()


def _seconds(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ReleaseError('worker_deadline_invalid')
    return value


class PreparedWorker:
    """One retained, fresh interpreter; cleanup never signals a resident."""
    def __init__(self, plugin_root, project, *, ready_seconds=30, run_seconds=1800):
        self.root = str(Path(plugin_root).resolve(strict=True))
        self.project = str(Path(project).resolve(strict=True))
        self.ready_seconds = _seconds(ready_seconds, 60)
        self.run_seconds = _seconds(run_seconds, 2100)
        self.buffer = bytearray()
        self.prepared = self.started = self.closed = False
        self.errors = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                 '--worker', self.root, self.project], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.errors, close_fds=True,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        except BaseException:
            self.errors.close()
            raise

    def _line(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    raise ReleaseError('worker_deadline_exceeded')
                block = os.read(self.process.stdout.fileno(), 65536)
                if not block:
                    raise ReleaseError('worker_protocol_invalid')
                self.buffer.extend(block)
                if len(self.buffer) > MAX_WIRE_BYTES:
                    raise ReleaseError('worker_output_too_large')
        line, _, tail = self.buffer.partition(b'\n')
        self.buffer = bytearray(tail)
        try:
            return line.decode('utf8')
        except UnicodeError:
            raise ReleaseError('worker_protocol_invalid') from None

    def ready(self):
        if self.closed or self.prepared or self.started:
            raise ReleaseError('worker_state_invalid')
        if self._line(time.monotonic() + self.ready_seconds) != 'READY':
            raise ReleaseError('worker_protocol_invalid')
        self.prepared = True

    def go(self):
        if self.closed or not self.prepared or self.started:
            raise ReleaseError('worker_state_invalid')
        self.started = True
        self.deadline = time.monotonic() + self.run_seconds
        try:
            self.process.stdin.write(b'GO\n')
            self.process.stdin.flush()
            self.process.stdin.close()
        except OSError:
            raise ReleaseError('worker_protocol_invalid') from None

    def finish(self):
        if self.closed or not self.started:
            raise ReleaseError('worker_state_invalid')
        try:
            value = json.loads(self._line(self.deadline))
            self.process.wait(timeout=max(.001, self.deadline - time.monotonic()))
        except (ValueError, subprocess.TimeoutExpired):
            raise ReleaseError('worker_result_invalid') from None
        if self.process.returncode != 0 or not isinstance(value, dict):
            raise ReleaseError('worker_failed')
        if (set(value) != {'project', 'results', 'owners'} or value['project'] != self.project
                or not isinstance(value['results'], list) or not isinstance(value['owners'], list)):
            raise ReleaseError('worker_result_invalid')
        rows = _completed_rows(value['results'])
        states = {r.get('stateRoot') for r in rows}
        owners = value['owners']
        if (not owners or any(not isinstance(r, dict) or set(r) != {'stateRoot', 'pluginRoot'}
                             or not isinstance(r['stateRoot'], str)
                             or not isinstance(r['pluginRoot'], str)
                             or r['pluginRoot'] != self.root for r in owners)
                or {r['stateRoot'] for r in owners} != states or len(owners) != len(states)):
            raise ReleaseError('preferred_owner_not_selected')
        return value

    def close(self):
        if self.closed:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    raise ReleaseError('worker_cleanup_unsettled') from None
        self.closed = True
        for pipe in (self.process.stdin, self.process.stdout, self.errors):
            try:
                pipe.close()
            except OSError:
                pass


def _worker(root, project):
    sys.path.insert(0, str(Path(root) / 'core'))
    import laya_bundle_upgrade as upgrade
    if getattr(upgrade, '_fixture', lambda: False)():
        return 75
    print('READY', flush=True)
    if sys.stdin.readline(16) != 'GO\n':
        return 75
    def expired(_signum, _frame):
        raise TimeoutError('bundle_upgrade_deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(1800)
    try:
        rows = upgrade.run(project, root=Path(root))
        finished = _completed_rows(rows)
        owners = [_preferred_owner(upgrade, row['stateRoot'], project, root, row['kind'])
                  for row in sorted(finished, key=lambda row: row['stateRoot'])]
        print(json.dumps({'project': project, 'results': rows, 'owners': owners}), flush=True)
        return 0
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        raise SystemExit(2)
    raise SystemExit(_worker(*sys.argv[2:]))
