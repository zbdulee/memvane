"""Public application orchestration operations."""
from __future__ import annotations
from importlib import import_module as _import_module


class _RuntimeDispatchOperations:
    def __getattr__(self, name):
        return getattr(_import_module('memvane.app.runtime_dispatch'), name)


_runtime_dispatch = _RuntimeDispatchOperations()
