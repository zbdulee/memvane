"""Public extractor context rendering."""
from __future__ import annotations


def render_existing_context_section(*args, **kwargs):
    from memvane.wiki.engines.extractors.lib import render_existing_context_section as implementation
    return implementation(*args, **kwargs)
