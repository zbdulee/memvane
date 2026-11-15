"""Public wiki compile operations."""
from __future__ import annotations


def run(project):
    from memvane.wiki.compile.selected_execution import run as implementation
    return implementation(project)
