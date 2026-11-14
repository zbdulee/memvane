"""Public lifecycle operations."""
from __future__ import annotations

from memvane.kernel.paths import GIT_TIMEOUT_SECONDS
from memvane.wiki.lifecycle.types import UNTRACKED


def tracked_states(root, paths, head_oid, *, budget=GIT_TIMEOUT_SECONDS):
    from memvane.wiki.lifecycle.git_branch_safety import tracked_states as implementation
    return implementation(root, paths, head_oid, budget=budget)


def run_lifecycle(project):
    from memvane.wiki.lifecycle.wiki_lifecycle_request import run_lifecycle as implementation
    return implementation(project)


def retire_deleted_source(root, config, selected, collection, rel, prior_hash, *, lineage=None):
    from memvane.wiki.lifecycle.wiki_source_retire import retire_deleted_source as implementation
    return implementation(root, config, selected, collection, rel, prior_hash, lineage=lineage)
