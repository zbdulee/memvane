"""Explicit activation of a staged, owner-managed learning runtime."""
import os
from pathlib import Path

from memvane.kernel.resource_deferred import ResourceDeferred


def _memory_admission():
    from memvane.learning.context_learning.resident import _admission
    limits, _report = _admission(8192, already_running=False)
    return limits is not None


def _activate(executable, model_dir):
    from memvane.learning.context_learning.runtime_installer import activate_generation
    from memvane.learning.context_learning.runtime_setup import default_managed_root
    from memvane.learning.context_learning.selector_heal import running_adapter
    return activate_generation(default_managed_root(), executable, str(running_adapter()), model_dir)


def needs_activation():
    from memvane.learning.context_learning.runtime_setup import _activated, default_managed_root
    return not _activated(default_managed_root())


def activate(executable, model_dir):
    if not Path(executable).is_absolute() or not Path(model_dir).is_absolute():
        raise ValueError('invalid_compatibility_source_path')
    if not _memory_admission():
        raise ResourceDeferred('learning_prepare_memory_pressure')
    from memvane.learning.context_learning.selector_heal import _prepare_lease, HealDeferred
    try:
        lease = _prepare_lease(None)
    except HealDeferred as exc:
        raise ResourceDeferred(str(exc)) from exc
    try:
        return _activate(executable, model_dir)
    finally:
        os.close(lease)
