"""Public wiki compile operations."""
from __future__ import annotations


def run(*args, **kwargs):
    from memvane.wiki.compile.selected_execution import run as implementation
    return implementation(*args, **kwargs)
