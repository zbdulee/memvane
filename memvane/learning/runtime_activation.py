"""Select a staged learning runtime; prove it off-hook before first use."""
import os
import re
import stat
from pathlib import Path

from memvane.kernel.resource_deferred import ResourceDeferred

PENDING = 'pending-activation.json'
FAILURE = 'activation-failure.json'


def _memory_admission():
    from memvane.learning.context_learning.resident import _admission
    limits, _report = _admission(8192, already_running=False)
    return limits is not None


def _root():
    from memvane.learning.context_learning.runtime_setup import default_managed_root
    return default_managed_root()


def _pending():
    from memvane.learning.context_learning.dataset import read_private_json
    path = _root() / PENDING
    if not path.exists() and not path.is_symlink():
        return None
    row = read_private_json(path)
    keys = {'schema', 'executable', 'model_dir', 'model_identity', 'runtime_identity_sha256'}
    if (not isinstance(row, dict) or set(row) != keys or row['schema'] != 'memvane-learning-pending-v1'
            or any(not isinstance(row[k], str) or not os.path.isabs(row[k]) for k in ('executable', 'model_dir'))
            or any(not isinstance(row[k], str) or not re.fullmatch('[0-9a-f]{64}', row[k])
                   for k in ('model_identity', 'runtime_identity_sha256'))):
        raise ValueError('invalid_pending_activation')
    return row


def _stage_pending(executable, model_dir):
    from memvane.learning.context_learning.runtime_installer import (
        _activation_lock, _generation_executable, _private_directory, _write_pointer)
    from memvane.learning.context_learning.dataset import read_private_json
    from memvane.learning.context_learning.adapter import model_identity
    root = _private_directory(_root())
    executable = _generation_executable(root, executable)
    prepared = read_private_json(executable.parent.parent.parent / 'prepared.json')
    identity = model_identity(model_dir)
    row = {'schema':'memvane-learning-pending-v1', 'executable':str(executable),
           'model_dir':str(model_dir), 'model_identity':identity,
           'runtime_identity_sha256':prepared['runtime_identity_sha256']}
    with _activation_lock(root):
        _generation_executable(root, executable)
        _write_pointer(root / PENDING, row)
        (root / FAILURE).unlink(missing_ok=True)
    return {'status':'activated_pending_proof', 'executable':str(executable),
            'runtime_identity_sha256':row['runtime_identity_sha256']}


def _activate_pending(row):
    from memvane.learning.context_learning.runtime_installer import activate_generation
    from memvane.learning.context_learning.selector_heal import running_adapter
    return activate_generation(_root(), row['executable'], str(running_adapter()), row['model_dir'],
                               expected_pending=row)


def _mark_failed(row, reason):
    from memvane.learning.context_learning.runtime_installer import _activation_lock, _write_pointer
    root = _root()
    with _activation_lock(root):
        if _pending() != row:
            return
        _write_pointer(root / FAILURE, {'schema':'memvane-learning-activation-failure-v1',
                                       'executable':row['executable'], 'reason':reason[:128]})
        (root / PENDING).unlink()


def failure_notice():
    from memvane.learning.context_learning.dataset import read_private_json
    try:
        row = read_private_json(_root() / FAILURE)
        return isinstance(row, dict) and row.get('schema') == 'memvane-learning-activation-failure-v1'
    except (OSError, ValueError, TypeError):
        return False


def has_active_runtime():
    from memvane.learning.context_learning.runtime_setup import _activated
    return _activated(_root())


def _discard_invalid_pending():
    """Under the existing activation lock, discard only a safe malformed file."""
    from memvane.learning.context_learning.runtime_installer import _activation_lock, _private_directory, _write_pointer
    root = _private_directory(_root())
    path = root / PENDING
    with _activation_lock(root):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 16 * 1024 * 1024):
            raise ValueError('unsafe_input_file')
        try:
            _pending()
        except (ValueError, UnicodeError) as exc:
            if str(exc) in ('unsafe_input_file', 'input_changed', 'input_budget_exceeded'):
                raise
        else:
            return False
        after = path.lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ResourceDeferred('learning_pending_changed')
        _write_pointer(root / FAILURE, {'schema': 'memvane-learning-activation-failure-v1',
                                        'executable': '', 'reason': 'invalid_pending_activation'})
        path.unlink()
        return True


def needs_activation():
    from memvane.learning.context_learning.runtime_setup import _activated
    try:
        if _pending() is not None:
            return False
    except (OSError, ValueError, TypeError):
        pass
    return not _activated(_root())


def activate(executable, model_dir):
    if not Path(executable).is_absolute() or not Path(model_dir).is_absolute():
        raise ValueError('invalid_compatibility_source_path')
    return _stage_pending(executable, model_dir)


def ensure_pending(*, decision=None, project=None):
    try:
        return _ensure_pending(decision=decision, project=project)
    except ResourceDeferred as exc:
        from memvane.diagnostics import api as diagnostics
        report = exc.report if isinstance(exc.report, dict) else {}
        admission = report.get('memory_admission') or {}
        reserve, peak = admission.get('reserve_mib'), admission.get('required_additional_mib')
        required = reserve + peak if type(reserve) in (int, float) and type(peak) in (int, float) else None
        diagnostics.emit('learning.runtime.proof', outcome='deferred', decision=decision,
                         reason=str(exc), detail=report.get('reason'), requiredMib=required)
        raise


def _ensure_pending(*, decision=None, project=None):
    try:
        row = _pending()
    except (ValueError, UnicodeError):
        if not _discard_invalid_pending():
            raise ResourceDeferred('learning_pending_changed')
        from memvane.diagnostics import api as diagnostics
        diagnostics.emit('learning.runtime.proof', outcome='failed', decision=decision,
                         reason='invalid_pending_activation')
        return {'status': 'invalid_pending'}
    if row is None:
        return {'status':'no_pending'}
    if decision is None and project is not None:
        from memvane.journal.api import select_route
        decision = select_route(str(project), action='update')
    if not _memory_admission():
        raise ResourceDeferred('learning_prepare_memory_pressure')
    from memvane.learning.context_learning.selector_heal import _prepare_lease, HealDeferred
    try:
        lease = _prepare_lease(None)
    except HealDeferred as exc:
        raise ResourceDeferred(str(exc)) from exc
    try:
        current = _pending()
        if current is None:
            return {'status':'no_pending'}
        if current != row:
            raise ResourceDeferred('learning_pending_changed')
        try:
            result = _activate_pending(row)
        except ResourceDeferred:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _mark_failed(row, str(exc))
            from memvane.diagnostics import api as diagnostics
            diagnostics.emit('learning.runtime.proof', outcome='failed', decision=decision, reason=str(exc))
            raise
        from memvane.diagnostics import api as diagnostics
        diagnostics.emit('learning.runtime.proof', outcome='accepted', decision=decision)
        return result
    finally:
        os.close(lease)
