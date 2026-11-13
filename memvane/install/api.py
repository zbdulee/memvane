"""Public installation operations."""
from __future__ import annotations


def cancel_wake(*args, **kwargs):
    from memvane.install.runtime_wake_cancel import cancel as implementation
    return implementation(*args, **kwargs)


def wake_status(*args, **kwargs):
    from memvane.install.runtime_wake_cancel import status as implementation
    return implementation(*args, **kwargs)


class _ModuleOperations:
    def __init__(self, module): self.module = module
    def __getattr__(self, name):
        from importlib import import_module
        return getattr(import_module('memvane.install.' + self.module), name)

_initial_build = _ModuleOperations('initial_build')
_ledger = _ModuleOperations('ledger_common')


def install_lock(*args, **kwargs):
    from memvane.install.locks import install_lock as implementation
    return implementation(*args, **kwargs)


def build_lookup(*args, **kwargs):
    from memvane.install.initial_build import lookup as implementation
    return implementation(*args, **kwargs)


def build_owner(*args, **kwargs):
    from memvane.install.initial_build import owner as implementation
    return implementation(*args, **kwargs)


def build_advance_v1(*args, **kwargs):
    from memvane.install.initial_build import advance_v1 as implementation
    return implementation(*args, **kwargs)
