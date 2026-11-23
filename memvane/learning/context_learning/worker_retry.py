"""The end of a worker request that keeps asking to be woken again.

An owned worker child that ends with a retry result (``memvane.runtime.types.RETRY``:
resources not ready, a deferred evaluation or application) leaves its request
pending and asks for another ``learning`` wake. ``again`` counts such results in
a row in ``worker-retry.json``, whatever their status: resource deferrals and a
busy lock come and go with the machine's load and must not hide an application
or evaluation that never gets done. At ``RETRY_RESULT_LIMIT`` the request is
settled with the last status instead (no further wake) and one
``learning.cycle`` row says so (``retry_cap`` with that status as its reason
and the count). The next hook launch writes a new request, and the count
starts over. A result that is not a retry clears the count.
"""
from __future__ import annotations

from pathlib import Path

NAME = 'worker-retry.json'
# About 24 hours of passes at the wake's 300-second backoff ceiling.
RETRY_RESULT_LIMIT = 288


def _count(path):
    from .dataset import read_private_json
    try:
        row = read_private_json(path) if path.exists() else None
    except (OSError, ValueError, TypeError):
        row = None
    kept = isinstance(row, dict) and type(row.get('count')) is int
    return (row['count'] if kept and row['count'] > 0 else 0) + 1


def _clear(path):
    try:
        path.unlink()
    except OSError:
        pass  # Ignored: a count that stays is only reached sooner, and the next result rewrites it.


def again(root, project, status, retryable):
    """Whether this result keeps the request pending and asks for another wake."""
    path = Path(root) / NAME
    if not retryable:
        _clear(path)
        return False
    count = _count(path)
    if count < RETRY_RESULT_LIMIT:
        from .local_cycle import _atomic_json
        try:
            _atomic_json(path, {'schema': 1, 'status': status, 'count': count})
        except OSError:
            pass  # Ignored: the count is not advanced, so the limit comes one result later.
        return True
    _clear(path)
    try:
        from memvane.diagnostics.api import emit
        from memvane.journal.api import select_route
        emit('learning.cycle', outcome='retry_cap', reason=status, retries=count,
             decision=select_route(str(project), action='update'))
    except Exception:
        pass  # Diagnostic only: the settled request is the record.
    return False
