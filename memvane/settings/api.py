"""Public project settings operations."""
from __future__ import annotations


def auxiliary_wiki_row(*args, **kwargs):
    from memvane.settings.source_text import auxiliary_wiki_row as implementation
    return implementation(*args, **kwargs)
