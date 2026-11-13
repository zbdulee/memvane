"""Public lifecycle operations."""
from __future__ import annotations

from memvane.wiki.lifecycle.types import UNTRACKED


def tracked_states(*args, **kwargs):
    from memvane.wiki.lifecycle.git_branch_safety import tracked_states as implementation
    return implementation(*args, **kwargs)


def run_lifecycle(*args, **kwargs):
    from memvane.wiki.lifecycle.wiki_lifecycle_request import run_lifecycle as implementation
    return implementation(*args, **kwargs)


def retire_deleted_source(*args, **kwargs):
    from memvane.wiki.lifecycle.wiki_source_retire import retire_deleted_source as implementation
    return implementation(*args, **kwargs)
