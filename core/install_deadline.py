"""One bounded lock budget for explicit setup, including nested registries.

Ordinary hook/worker locking is unchanged outside this in-process context.
Busy holders are retained. Permission and unsafe-state errors are not busy.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import math
import time

_deadline = ContextVar('qmd_explicit_install_deadline', default=None)

class InstallBusy(TimeoutError):
    pass

def active():
    """Whether explicit installation supplied a finite nested wait budget."""
    return _deadline.get() is not None

@contextmanager
def operation(seconds):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise ValueError('invalid_install_deadline')
    until = time.monotonic() + seconds
    parent = _deadline.get()
    if parent is not None:
        until = min(parent, until)
    token = _deadline.set(until)
    try:
        yield until
    finally:
        _deadline.reset(token)

def wait_deadline(seconds):
    until = time.monotonic() + seconds
    parent = _deadline.get()
    return until if parent is None else min(parent, until)

def flock(fd, flags):
    until = _deadline.get()
    if until is None or flags & (fcntl.LOCK_UN | fcntl.LOCK_NB):
        return fcntl.flock(fd, flags)
    while True:
        try:
            # One immediate attempt also permits exact-owned cleanup after
            # expiry, but a contended lock can never extend the budget.
            return fcntl.flock(fd, flags | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise InstallBusy('install_lock_busy') from exc
            time.sleep(min(.05, remaining))
