"""One monotonic budget shared by prompt and post-tool recall subprocesses."""
import math
import os
import signal
import time

DEFAULT_SECONDS = 18.0
MIN_SECONDS = 2.0
MAX_SECONDS = 20.0
DEFERRED_REPLAY_SECONDS = 55.0
DEFERRED_REPLAY_MAX_SECONDS = 60.0
_deadline = None


class HookDeadlineExceeded(BaseException):
    pass


def deferred_replay():
    """Only a ticket-bound local capture replay may use the background budget."""
    ticket = os.environ.get('QMD_DEFERRED_CAPTURE_TICKET', '')
    return (os.environ.get('QMD_EDIT_INTERNAL') == '1'
            and os.environ.get('QMD_DEFERRED_CAPTURE_REPLAY') == '1'
            and len(ticket) == 32
            and all(ch in '0123456789abcdef' for ch in ticket))


def _seconds():
    replay = deferred_replay()
    default = DEFERRED_REPLAY_SECONDS if replay else DEFAULT_SECONDS
    maximum = DEFERRED_REPLAY_MAX_SECONDS if replay else MAX_SECONDS
    try:
        value = float(os.environ.get('QMD_HOOK_TOTAL_SECONDS', default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and MIN_SECONDS <= value <= maximum else default


def arm(*, watchdog=True):
    """Start once at the outer hook; a child inherits the absolute deadline."""
    global _deadline
    if _deadline is None:
        try:
            inherited = float(os.environ.get('QMD_HOOK_DEADLINE_MONO', ''))
        except (TypeError, ValueError):
            inherited = 0
        now = time.monotonic()
        # A child must never reset an already exhausted parent budget.
        maximum = DEFERRED_REPLAY_MAX_SECONDS if deferred_replay() else MAX_SECONDS
        _deadline = inherited if math.isfinite(inherited) and 0 < inherited <= now + maximum else now + _seconds()
        os.environ['QMD_HOOK_DEADLINE_MONO'] = str(_deadline)
    left = _deadline - time.monotonic()
    if left <= 0:
        raise HookDeadlineExceeded()
    if watchdog and hasattr(signal, 'setitimer'):
        signal.signal(signal.SIGALRM, lambda _signum, _frame: (_ for _ in ()).throw(HookDeadlineExceeded()))
        signal.setitimer(signal.ITIMER_REAL, left)
    return _deadline


def remaining(cap=None):
    if _deadline is None:
        return cap if cap is not None else _seconds()
    left = _deadline - time.monotonic()
    if left <= 0:
        raise HookDeadlineExceeded()
    return min(left, cap) if cap is not None else left


def deadline():
    if _deadline is None:
        return float('inf')
    return _deadline
