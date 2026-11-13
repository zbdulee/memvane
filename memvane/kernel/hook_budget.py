"""One monotonic budget shared by prompt and post-tool recall subprocesses."""
import math
import os
import signal
import time

DEFAULT_SECONDS = 18.0
MIN_SECONDS = 2.0
MAX_SECONDS = 20.0
_deadline = None


class HookDeadlineExceeded(BaseException):
    pass


def _seconds():
    try:
        value = float(os.environ.get('QMD_HOOK_TOTAL_SECONDS', DEFAULT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    return (value if math.isfinite(value) and MIN_SECONDS <= value <= MAX_SECONDS
            else DEFAULT_SECONDS)


def arm(*, watchdog=True, export=True):
    """Start once at the outer hook; a child inherits the absolute deadline.

    ``export=False`` keeps the deadline in this process only.
    """
    global _deadline
    if _deadline is None:
        try:
            inherited = float(os.environ.get('QMD_HOOK_DEADLINE_MONO', ''))
        except (TypeError, ValueError):
            inherited = 0
        now = time.monotonic()
        # A child must never reset an already exhausted parent budget.
        _deadline = (inherited if math.isfinite(inherited) and 0 < inherited <= now + MAX_SECONDS
                     else now + _seconds())
        if export:
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
