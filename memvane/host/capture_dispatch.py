"""Post-output capture admission; the inherited share bounds both children."""
import json
import os
import subprocess
import tempfile


def dispatch(slot, started):
    pending, slot[:] = list(slot), []
    if not pending:
        return
    entry = pending[0]
    try:
        from memvane.kernel.bundle_paths import entry_argv
        from memvane.runtime.api import lease
        data = json.dumps({'capture': entry, 'started': started}, ensure_ascii=True,
                          allow_nan=False, separators=(',', ':')).encode('utf8')
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('capture_input_too_large')
        with lease() as budget, tempfile.TemporaryFile(mode='w+b') as payload:
            payload.write(data)
            payload.seek(0)
            env = dict(os.environ, MEMVANE_CAPTURE_FDS=','.join(str(fd) for fd in budget.fds))
            subprocess.Popen(entry_argv('learning-capture'), stdin=payload,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, pass_fds=budget.fds, start_new_session=True, cwd='/', env=env)
    except Exception as exc:
        from memvane.kernel.resource_deferred import ResourceDeferred
        try:
            from memvane.diagnostics.api import emit
            deferred = isinstance(exc, ResourceDeferred)
            reason = str(exc) if deferred and str(exc) in ('cpu_budget_busy', 'cpu_capacity_unavailable') else (
                'capture_budget_unavailable' if deferred else 'dispatch_failed')
            emit('learning.capture', outcome='deferred' if deferred else 'capture_unavailable',
                 reason=reason, decision=entry[1], host=entry[0],
                 editTarget=entry[2].get('_memvane_edit_target'))
        except Exception:
            pass
