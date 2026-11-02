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
COMPLETE = frozenset(('current', 'active_upgrade_complete', 'companion_sealed'))


class ReleaseError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


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
        rows = [r for r in value['results'] if isinstance(r, dict) and 'status' in r]
        if not rows or any(r['status'] not in COMPLETE for r in rows):
            raise ReleaseError('upgrade_incomplete')
        states = {r.get('stateRoot') for r in rows}
        owners = value['owners']
        if (not owners or any(not isinstance(r, dict) or set(r) != {'stateRoot', 'pluginRoot'}
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
        roots = sorted({r['stateRoot'] for r in rows if r.get('stateRoot') and 'status' in r})
        owners = []
        for state in roots:
            _kind, identity = upgrade.classify(Path(state))
            owners.append({'stateRoot': state, 'pluginRoot': identity.get('pluginRoot')})
        print(json.dumps({'project': project, 'results': rows, 'owners': owners}), flush=True)
        return 0
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        raise SystemExit(2)
    raise SystemExit(_worker(*sys.argv[2:]))
