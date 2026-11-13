"""Public wiki compile operations."""
from __future__ import annotations


def run(*args, **kwargs):
    from memvane.wiki.compile.selected_execution import run as implementation
    return implementation(*args, **kwargs)


from importlib import import_module as _import_module

class _CompileOperations:
    def __getattr__(self, name):
        for module in ('wiki_compile', 'wiki_compile_worker'):
            target = _import_module('memvane.wiki.compile.' + module)
            if hasattr(target, name):
                return getattr(target, name)
        raise AttributeError(name)

_operations = _CompileOperations()
