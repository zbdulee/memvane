"""Fixed per-kind wake handlers; runtime_work owns the queue, lease and retry.

Each handler revalidates setup/ownership/authorization itself right before its
side effect and returns True only when the wake may be ACKed. Owner modules are
imported inside the handler, so importing this table never loads learning code.
"""
from pathlib import Path

import runtime_wake_policy as wake_policy


def retired(row):
    """ACK a wake of a kind no handler serves any more (QMD index, update, cleanup).

    Older bundles queued these kinds. Acknowledging them drops the row once
    instead of retrying a missing handler forever.
    """
    return True


def _missing_target(row):
    """None while the project directory exists, else the missing outcome.

    A missing directory in the platform temp area (a removed test fixture
    project) is void and dropped at once. Anywhere else it may come back (an
    unmounted volume, a re-clone): ``TargetMissing`` backs off, and the queue
    drops the wake only after it stayed missing for a day
    (``runtime_wake_policy.MISSING_DROP_SECS``). A route that is merely not
    selected yet (``target_unavailable`` during a move) is not missing.
    """
    try:
        if Path(row['target']).is_dir():return None
    except (OSError, TypeError, ValueError):
        return None
    from runtime_lease import in_platform_temp
    if in_platform_temp(row['target']):return wake_policy.Dropped('target_missing')
    raise wake_policy.TargetMissing()


def laya_sync(row):
    missing=_missing_target(row)
    if missing is not None:return missing
    from laya_runtime_dispatch import run
    result=run('laya-sync', row['target'], owner_fd=row.get('leaseFd'))
    if result['state']=='indexed':
        # The Laya publish receipt replaces the QMD claim ACK as the
        # learning wake; it never changes this handler's own outcome.
        try:
            from context_learning.shared import wake_after_index
            wake_after_index(Path(row['target']))
        except Exception:
            pass
    return result['state'] in ('indexed','unchanged')


def laya_compile(row):
    missing=_missing_target(row)
    if missing is not None:return missing
    from laya_runtime_dispatch import run
    result=run('laya-compile', row['target'], owner_fd=row.get('leaseFd'))
    return result['state']=='drained'


def learning(row):
    import config
    from context_learning import shared
    from context_learning.laya_learning import ready
    project=Path(row['target'])
    if not project.is_dir():return True
    if not ready(project):return False
    cfg=config.find_project_config(str(project))['config'];root=shared._safe_root(cfg,project)
    if root is None:return True
    from context_learning.history_lifecycle import worker_lease
    with worker_lease():
        if not ready(project):return False
        # A hook-persisted full (teacher+trainer) request, else a training-only
        # retry (no teacher, no fresh captures), runs only in its owned child;
        # this dispatcher returns at once so index/update wakes keep priority.
        # The wake stays until that request settles.
        step=shared.learning_worker_step(root,project,row['revision'])
    return step['status'] not in ('running','started')


def auto(row):
    import context_learning_auto
    result=context_learning_auto.run(Path(row['target']))
    return wake_policy.handler_done(result['status'])


HANDLERS={'index':retired,'update':retired,'cleanup':retired,
          'laya-sync':laya_sync,'laya-compile':laya_compile,'learning':learning,'auto':auto}


def dispatch(row):
    """Fixed handlers revalidate setup/ownership/authorization themselves."""
    handler=HANDLERS.get(row['kind'])
    if handler is None:raise ValueError('invalid_runtime_handler')
    return handler(row)
