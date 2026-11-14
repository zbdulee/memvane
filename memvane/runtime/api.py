"""Public runtime operations: wake queue, sync owner, admission and resource policy.

Value types and the handle Protocols live in ``types.py``. A forwarder passes
an optional argument only when it differs from the default shown; otherwise
the implementation's own default applies.
"""
from __future__ import annotations as _annotations

# Wake queue.

def request(kind, target='', *, start=True, pull_due=False, delay=0):
    from memvane.runtime.runtime_work import request as implementation
    args = (kind, target) if target != '' else (kind,)
    given = {key: value for key, value, default in (
        ('start', start, True), ('pull_due', pull_due, False), ('delay', delay, 0))
        if value is not default and value != default}
    return implementation(*args, **given)

def hook_wake():
    from memvane.runtime.runtime_work import hook_wake as implementation
    return implementation()

def wake_queue(root=None):
    from memvane.runtime.runtime_work import WakeQueue as implementation
    args = (root,) if root is not None else ()
    return implementation(*args)

def runtime_main(argv, *, execute, on_drop=None, on_stall=None):
    from memvane.runtime.runtime_work import main as implementation
    given = {key: value for key, value, default in (
        ('on_drop', on_drop, None), ('on_stall', on_stall, None)) if value is not default}
    return implementation(argv, execute=execute, **given)

def handler_done(status):
    from memvane.runtime.runtime_wake_policy import handler_done as implementation
    return implementation(status)

def wake_cancel_status(kind, target, *, queue=None):
    from memvane.runtime.wake_cancel import status as implementation
    given = {'queue': queue} if queue is not None else {}
    return implementation(kind, target, **given)

# Sync owner.

def sync_owner(queue_path, *, recheck_queue=None, wait=False, deadline=None):
    from memvane.runtime.sync_owner_lock import sync_owner as implementation
    given = {key: value for key, value, default in (
        ('recheck_queue', recheck_queue, None), ('wait', wait, False), ('deadline', deadline, None))
        if value is not default}
    return implementation(queue_path, **given)

def validate_sync_owner(claim, queue_path):
    from memvane.runtime.sync_owner_lock import validate_sync_owner as implementation
    return implementation(claim, queue_path)

# Owner-private paths and locks.

def in_platform_temp(path):
    from memvane.runtime.runtime_lease import in_platform_temp as implementation
    return implementation(path)

def platform_temp_roots():
    from memvane.runtime.runtime_lease import platform_temp_roots as implementation
    return implementation()

def coordination_lock(path):
    from memvane.runtime.runtime_lease import coordination_lock as implementation
    return implementation(path)

def service_root(*, create=False, home=None):
    from memvane.runtime.service_state import service_root as implementation
    given = {key: value for key, value, default in (
        ('create', create, False), ('home', home, None)) if value is not default}
    return implementation(**given)

# Preparation and provider admission.

def preparation(*, clock=None, max_seconds=30):
    """``clock`` None means the implementation default ``time.monotonic``."""
    from memvane.runtime.runtime_admission import preparation as implementation
    given = {key: value for key, value, default in (
        ('clock', clock, None), ('max_seconds', max_seconds, 30))
        if value is not default and value != default}
    return implementation(**given)

def preparation_checkpoint():
    from memvane.runtime.runtime_admission import preparation_checkpoint as implementation
    return implementation()

def prepared(function):
    from memvane.runtime.runtime_admission import prepared as implementation
    return implementation(function)

def provider():
    from memvane.runtime.runtime_admission import provider as implementation
    return implementation()

def provider_popen(argv, *, before_launch=None, launch_failed=None, defer_launch=None, **options):
    from memvane.runtime.runtime_admission import provider_popen as implementation
    given = {key: value for key, value, default in (
        ('before_launch', before_launch, None), ('launch_failed', launch_failed, None),
        ('defer_launch', defer_launch, None))
        if value is not default}
    return implementation(argv, **given, **options)

def provider_run(argv, **options):
    from memvane.runtime.runtime_admission import provider_run as implementation
    return implementation(argv, **options)

def provider_job(function):
    from memvane.runtime.runtime_admission import provider_job as implementation
    return implementation(function)

def provider_call(function):
    from memvane.runtime.runtime_admission import provider_call as implementation
    return implementation(function)

def provider_checkpoint():
    from memvane.runtime.runtime_admission import provider_checkpoint as implementation
    return implementation()

def hash_file(path):
    from memvane.runtime.runtime_admission import hash_file as implementation
    return implementation(path)

def qa_budget_initialize(path, *, limit, used):
    from memvane.runtime.qa_model_budget import initialize as implementation
    return implementation(path, limit=limit, used=used)

def qa_budget_status(path, limit):
    from memvane.runtime.qa_model_budget import status as implementation
    return implementation(path, limit)

def reserve_qa_budget():
    from memvane.runtime.qa_model_budget import reserve_from_env as implementation
    return implementation()

# CPU share.

def configure_torch(torch):
    from memvane.runtime.runtime_cpu import configure_torch as implementation
    return implementation(torch)

def environment(base=None):
    from memvane.runtime.runtime_cpu import environment as implementation
    args = (base,) if base is not None else ()
    return implementation(*args)

def lease(root=None):
    from memvane.runtime.runtime_cpu import lease as implementation
    args = (root,) if root is not None else ()
    return implementation(*args)

# Resource coordination and calibration.

def coordinator(root=None, *, clock=None, wall=None):
    """``clock``/``wall`` None mean the implementation defaults ``time.monotonic``/``time.time``."""
    from memvane.runtime.resource_coordination import Coordinator as implementation
    args = (root,) if root is not None else ()
    given = {key: value for key, value, default in (
        ('clock', clock, None), ('wall', wall, None)) if value is not default}
    return implementation(*args, **given)

def resource_report(policy, *, detailed=False, admission=True, deadline=None):
    from memvane.runtime.resource_report import report as implementation
    given = {key: value for key, value, default in (
        ('detailed', detailed, False), ('admission', admission, True), ('deadline', deadline, None))
        if value is not default}
    return implementation(policy, **given)

def observation_fresh(report, *, now=None):
    from memvane.runtime.resource_coordination import observation_fresh as implementation
    given = {'now': now} if now is not None else {}
    return implementation(report, **given)

def admission_policy(policy):
    from memvane.runtime.resource_coordination import admission_policy as implementation
    return implementation(policy)

def training_guard(control):
    from memvane.runtime.resource_coordination import TrainingGuard as implementation
    return implementation(control)

def memory_validate(value):
    from memvane.runtime.memory_policy import validate as implementation
    return implementation(value)

def check_mps_memory(torch, limits):
    from memvane.runtime.resource_controls import MpsMemoryControl
    return MpsMemoryControl.check(torch, limits)

def supervisor(root, *, factory=None, clock=None):
    """``clock`` None means the implementation default ``time.monotonic``."""
    from memvane.runtime.runtime_monitor import Supervisor as implementation
    given = {key: value for key, value, default in (
        ('factory', factory, None), ('clock', clock, None)) if value is not default}
    return implementation(root, **given)

def calibration_verdict(report, policy, *, now=None):
    from memvane.runtime.bounded_calibration import verdict as implementation
    given = {'now': now} if now is not None else {}
    return implementation(report, policy, **given)

def calibration_reserve_launch(request):
    from memvane.runtime.bounded_calibration import reserve_launch as implementation
    return implementation(request)

def validate_calibration_request(request, *, now=None):
    from memvane.runtime.bounded_calibration import validate_request as implementation
    given = {'now': now} if now is not None else {}
    return implementation(request, **given)

def load_calibration(path, command):
    from memvane.runtime.bounded_calibration import load as implementation
    return implementation(path, command)

def calibration_sha(value):
    from memvane.runtime.bounded_calibration import _sha as implementation
    return implementation(value)
