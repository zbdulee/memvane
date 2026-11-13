"""Public project settings operations."""
from __future__ import annotations


def auxiliary_wiki_row(*args, **kwargs):
    from memvane.settings.source_text import auxiliary_wiki_row as implementation
    return implementation(*args, **kwargs)


def compile_rel(*args, **kwargs):
    from memvane.settings.project_dir import compile_rel as implementation
    return implementation(*args, **kwargs)



def compile_dir(*args, **kwargs):
    from memvane.settings.project_dir import compile_dir as implementation
    return implementation(*args, **kwargs)
