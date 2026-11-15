"""Read-only status of a project's ``laya-sync`` build wake.

The queue read (the wake row, its overflow flag and any recorded cancel
audit rows) is ``runtime.wake_cancel`` behind the runtime api.
"""
from __future__ import annotations

KIND = 'laya-sync'


def status(project_raw, *, queue=None):
    """Read-only: the current wake row for this project and its cancel audit."""
    from memvane.runtime.api import wake_cancel_status
    return wake_cancel_status(KIND, str(project_raw), queue=queue)
