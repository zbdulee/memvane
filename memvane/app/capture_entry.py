"""Private post-hook capture supervisor and worker entries."""
import json
import os
import stat
import sys
from contextlib import contextmanager

CAPTURE_SECONDS = 45
STOP_SECONDS = 5


@contextmanager
def _budget():
    raw = os.environ.get('MEMVANE_CAPTURE_FDS', '')
    if raw:
        if len(raw) > 512:
            raise ValueError('capture_budget_invalid')
        fds = tuple(int(value) for value in raw.split(','))
        if not 1 <= len(fds) <= 64 or any(fd < 3 for fd in fds) or len(set(fds)) != len(fds):
            raise ValueError('capture_budget_invalid')
        for fd in fds:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('capture_budget_invalid')
        yield fds
    else:
        from memvane.runtime.api import lease
        with lease() as budget:
            yield budget.fds


def _capture_input():
    raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('capture_input_too_large')
    row = json.loads(raw.decode('utf8'))
    capture, started = row['capture'], row['started']
    if not isinstance(capture, list) or len(capture) != 6 or type(started) not in (int, float):
        raise ValueError('capture_input_invalid')
    return capture, started


def _edit_target(capture):
    """``main`` or ``linked`` of the capture input's edit, for the event row."""
    value = capture[2].get('_memvane_edit_target') if isinstance(capture[2], dict) else None
    return value if value in ('main', 'linked') else None


def learning_capture_worker():
    try:
        capture, _started = _capture_input()
        from memvane.learning.api import capture_recall
        # The detached worker is bounded by the supervisor's CAPTURE_SECONDS,
        # not by the hook's start time: that ``started`` is another process's
        # monotonic clock, which macOS system Python 3.9 starts near zero in
        # every process, so comparing it here measured nothing (3.9) or
        # dropped slow-selector edit cases (3.13).
        capture_recall(*capture)
        return 0  # The capture API records every handled status itself.
    except (ValueError, TypeError, KeyError, UnicodeError):
        return 2


def learning_capture():
    """Observe the capture child so an abnormal exit is recorded per install."""
    import subprocess
    try:
        capture, _started = _capture_input()
    except (ValueError, TypeError, KeyError, UnicodeError):
        return 2
    reason = 'worker_failed'
    try:
        from memvane.kernel.bundle_paths import entry_argv
        sys.stdin.buffer.seek(0)
        with _budget() as fds:
            child = subprocess.Popen(entry_argv('learning-capture-worker'), stdin=sys.stdin,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     close_fds=True, pass_fds=fds)
            try:
                code = child.wait(timeout=CAPTURE_SECONDS)
            except subprocess.TimeoutExpired:
                reason = 'worker_timeout'
                child.terminate()
                try:
                    child.wait(timeout=STOP_SECONDS)
                except subprocess.TimeoutExpired:
                    child.kill()
                    try:
                        child.wait(timeout=STOP_SECONDS)
                    except subprocess.TimeoutExpired:
                        reason = 'worker_cleanup_timeout'
                code = None
        if code == 0:
            return 0
    except (OSError, ValueError) as exc:
        from memvane.kernel.resource_deferred import ResourceDeferred
        if isinstance(exc, ResourceDeferred):
            try:
                from memvane.diagnostics.api import emit
                emit('learning.capture', outcome='deferred', reason='capture_budget_unavailable',
                     decision=capture[1], host=capture[0], editTarget=_edit_target(capture))
            except Exception:
                pass
            return 0
        reason = 'worker_spawn_failed'
    try:
        from memvane.diagnostics.api import emit
        emit('learning.capture', outcome='lost', reason=reason,
             decision=capture[1], host=capture[0], editTarget=_edit_target(capture))
    except Exception:
        pass
    return 2
