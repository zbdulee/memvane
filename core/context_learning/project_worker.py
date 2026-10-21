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


def run(root, project, config=None):
    from . import shared
    import config as qmd_config
    root,project=Path(root),Path(project).resolve(strict=True)
    config=config if config is not None else qmd_config.find_project_config(str(project))['config']
    flags=config.get('contextLearning',{})
    if (flags.get('capture') is not True or flags.get('autoCycle') is not True
            or config.get('indexing') is False or shared._safe_root(config,project)!=root):
        return {'status':'disabled'}
    if not setup_ready(root):return {'status':'setup_in_progress'}
    from . import project_scope,general_teacher
    import project_learning_setup
    from .resource_coordination import _lock
    policy=shared._policy(root)
    fd=_lock(root/'project-worker.lock')
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        report=shared.resource_report(policy,detailed=True)
        if not report['ready']:
            return {'status':'deferred_resource','resources':report}
        from .local_cycle import ResourceDeferred
        try:project_scope.require(root,policy,'local_train',fresh_inventory=True)
        except ResourceDeferred as exc:return {'status':'deferred_resource','reason':str(exc)}
        if project_learning_setup.synthetic_local_only(root,project,policy):
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
        cycle=shared.run_training_pending(root,project)
        result={'status':cycle['status'],'teacher':teacher,'cycle':cycle,
                'quota':quota,
                'activeCheckpointChanged':cycle['status'] in ('applied','already_applied')}
        from .local_cycle import _atomic_json
        _atomic_json(root/'project-last-status.json',result)
        return result
    finally:os.close(fd)
