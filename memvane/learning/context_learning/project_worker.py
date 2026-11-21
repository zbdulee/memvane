"""One bounded, explicitly enrolled project worker; never a login service."""
import fcntl
import os
from pathlib import Path


def setup_ready(root):
    from .dataset import read_private_json
    path=Path(root)/'project-learning-setup.json'
    if not path.exists() and not path.is_symlink():return True
    try:return read_private_json(path).get('phase')=='activated'
    except (OSError,ValueError,TypeError,AttributeError):return False


def _retire_prompt_cases(root, project, policy, config):
    """Default namespaces delete retired prompt cases once per profile kind (decision 10)."""
    from memvane.learning.default_enrollment import default_local_only, default_teacher
    try:
        if default_teacher(root, project, policy):
            kind = 'default-teacher'
        elif default_local_only(root, project, policy):
            kind = 'default-local'
        else:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    from .prompt_retirement import retire_quietly
    return retire_quietly(root, project, config, profile=kind)


def _delete_unchecked_edit_cases(root, project, config):
    """Pre-fix edit cases of a project inside a parent work tree go once (decision 8)."""
    from .edit_git_migration import migrate_quietly
    return migrate_quietly(root, project, config)


def _default_teacher_pass(root, project, policy):
    """The default teacher's pass as a status; only scope/setup refusals raise (§11-1)."""
    from . import general_teacher
    from .local_cycle import ResourceDeferred
    try:
        return general_teacher.process_pending_default(root, project, policy, max_cases=8)
    except ResourceDeferred as exc:
        return {'status': 'deferred_resource', 'reason': str(exc)[:80]}
    except ValueError as exc:
        if str(exc).startswith(general_teacher.SCOPE_ERRORS):
            raise
        return {'status': 'teacher_failed', 'reason': str(exc)[:80]}
    except Exception as exc:
        return {'status': 'teacher_failed', 'reason': type(exc).__name__}


def _number(value):
    import math
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _record_deferral(root, project, policy, cycle, started):
    """One ``learning.cycle`` row for a cycle the resource admission deferred; never raises.

    The inner reason and the numbers come from this pass's ``resource-defer.json``
    (admission result and the observation it judged). Counts and MiB only.
    """
    try:
        from .dataset import read_private_json
        try:
            defer = read_private_json(Path(root) / 'resource-defer.json')
            if not isinstance(defer, dict) or not _number(defer.get('recordedAt')) or defer['recordedAt'] < started:
                defer = {}
        except (OSError, ValueError):
            defer = {}
        resources = defer.get('resources') if isinstance(defer.get('resources'), dict) else {}
        observed = resources.get('observed') if isinstance(resources.get('observed'), dict) else resources
        admission = resources.get('memory_admission') if isinstance(resources.get('memory_admission'), dict) else {}
        reserve, extra = _number(admission.get('reserve_mib')), _number(admission.get('required_additional_mib'))
        latch = None
        try:
            from memvane.runtime.api import coordinator as Coordinator
            state = read_private_json(Coordinator().root / 'resource-recovery.json')
            latch = state.get('paused') if isinstance(state, dict) and type(state.get('paused')) is bool else None
        except (OSError, ValueError, TypeError):
            pass
        reason = resources.get('reason') or defer.get('reason') or cycle.get('reason')
        from memvane.journal.api import select_route
        from memvane.diagnostics.api import emit
        emit('learning.cycle', decision=select_route(str(project), action='update'),
             outcome='deferred_resource', reason=str(reason)[:80] if reason else None,
             deferredBy=str(defer.get('reason'))[:80] if defer.get('reason') else None,
             availableMib=_number(observed.get('available_mib')),
             requiredMib=reserve + extra if reserve is not None and extra is not None else None,
             loadPerCpu=_number(observed.get('load_per_cpu')),
             loadLimit=_number(policy.get('maxLoadPerCpu')), latchPaused=latch)
    except Exception:
        pass  # Diagnostic only: the cycle's own retry state is the record.


def run(root, project, config=None):
    from . import shared
    from memvane.settings import api as qmd_config
    root,project=Path(root),Path(project).resolve(strict=True)
    config=config if config is not None else qmd_config.find_project_config(str(project))['config']
    flags=config.get('contextLearning',{})
    if (flags.get('capture') is not True or flags.get('autoCycle') is not True
            or config.get('indexing') is False or shared._safe_root(config,project)!=root):
        return {'status':'disabled'}
    if not setup_ready(root):return {'status':'setup_in_progress'}
    from . import project_scope,general_teacher
    from memvane.learning import project_learning_setup
    from memvane.learning.default_enrollment import profile_kind
    from memvane.runtime.api import coordination_lock as _lock
    policy=shared._policy(root)
    fd=_lock(root/'project-worker.lock')
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        _retire_prompt_cases(root,project,policy,config)
        _delete_unchecked_edit_cases(root,project,config)
        report=shared.resource_report(policy,detailed=True)
        if not report['ready']:
            return {'status':'deferred_resource','resources':report}
        from .local_cycle import ResourceDeferred
        try:grant=project_scope.require(root,policy,'local_train',fresh_inventory=True)
        except ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
        kind=profile_kind(root,project,policy)
        if kind=='default-local':
            teacher={'status':'disabled_local_only'}
            quota={'status':'not_applicable_local_only'}
        elif kind=='default-teacher':
            from . import prequential
            from .case_rotation import rotate_quietly
            # Ignored on failure: nothing was deleted and the next pass plans again
            # (the delete itself is checked inside its transaction).
            rotate_quietly(root,project,grant)
            # Before the teacher, whatever its state: a late gold keeps the capture-time kind.
            try:prequential.prerecord_selections(root,project,grant)
            except Exception:pass  # Diagnostic only; the next pass looks again.
            teacher=_default_teacher_pass(root,project,policy)
            if teacher['status'] in ('busy','deferred_resource'):
                return {'status':teacher['status'],'teacher':teacher}
            quota={'status':'not_applicable_default_teacher'}
            # Recovery: a rejected journal a stopped pass left open is closed before the cycle.
            try:prequential.close_rejected(root,grant)
            except Exception:pass  # Retried next pass; the cycle re-rejects it meanwhile.
        elif kind=='synthetic':
            teacher={'status':'disabled_synthetic_local_only'}
            quota={'status':'not_applicable_local_only'}
            budget=project_learning_setup.synthetic_budget_status(root,project,policy)
            if budget['status']!='ready':
                return {'status':budget['status'],'teacher':teacher,'cycle':budget,
                        'quota':quota,'activeCheckpointChanged':False}
        else:
            teacher=general_teacher.process_pending(root,project,policy,max_cases=1)
            # A quota/consent uncertainty cannot become a new local model call.
            if teacher['status'] in ('deferred_quota','deferred_resource','consent_required','busy'):
                return {'status':teacher['status'],'teacher':teacher}
            try:
                quota=general_teacher.read_weekly_quota()
            except ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
            except ValueError as exc:
                return {'status':'deferred_quota','reason':str(exc) if str(exc) in (
                    'weekly_quota_checkpoint','weekly_quota_unavailable') else 'weekly_quota_unavailable',
                    'teacher':teacher}
        import time
        started=time.time()
        cycle=shared.run_training_pending(root,project)
        if cycle.get('status')=='deferred_resource':
            _record_deferral(root,project,policy,cycle,started)
        if kind=='default-teacher':
            from . import prequential
            prequential.record_pass(root,project,grant,cycle)
        result={'status':cycle['status'],'teacher':teacher,'cycle':cycle,
                'quota':quota,
                'activeCheckpointChanged':cycle['status'] in ('applied','already_applied')}
        from .local_cycle import _atomic_json
        _atomic_json(root/'project-last-status.json',result)
        return result
    finally:os.close(fd)
