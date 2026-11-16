"""Private post-hook capture supervisor and worker entries."""
import json
import sys


def _capture_input():
    raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('capture_input_too_large')
    row = json.loads(raw.decode('utf8'))
    capture, started = row['capture'], row['started']
    if not isinstance(capture, list) or len(capture) != 6 or type(started) not in (int, float):
        raise ValueError('capture_input_invalid')
    return capture, started


def learning_capture_worker():
    try:
        capture, started = _capture_input()
        from memvane.learning.api import capture_recall
        status = capture_recall(*capture, started=started)
        return 2 if status in ('capture_unavailable', 'capture_deadline') else 0
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
        child = subprocess.Popen(entry_argv('learning-capture-worker'), stdin=sys.stdin,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        try:
            code = child.wait(timeout=45)
        except subprocess.TimeoutExpired:
            reason = 'worker_timeout'
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            code = None
        if code == 0:
            return 0
    except (OSError, ValueError):
        reason = 'worker_spawn_failed'
    try:
        from memvane.diagnostics.api import emit
        emit('learning.capture', outcome='lost', reason=reason,
             decision=capture[1], host=capture[0])
    except Exception:
        pass
    return 2
