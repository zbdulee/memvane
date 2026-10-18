"""Pure Candidate C wiki membership planning; no QMD or filesystem access.

A view is a *complete* inventory of wiki files actually observed in one
worktree. Branch names are provenance only. Missing files are deletions, except
when the scanner explicitly reports a path/prefix as unreadable. An immutable
generation can be retained while an in-flight request is pinned to it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class WikiView:
    worktree: str
    generation: str  # wiki -> search publication; never a moving HEAD
    raw_generation: str  # raw -> wiki source/materialisation provenance
    members: tuple[tuple[str, str], ...]  # relative wiki path -> content hash
    branch_hint: str | None = None


@dataclass(frozen=True)
class MembershipPlan:
    expected_generation: str | None
    view: WikiView
    added: tuple[str, ...]
    edited: tuple[str, ...]
    deleted: tuple[str, ...]
    # A rename is represented by delete+add; hash reuse needs no embedding.


def _valid_text(value):
    return isinstance(value, str) and bool(value) and '\x00' not in value


def _valid_path(path):
    return (_valid_text(path) and not path.startswith('/') and '\\' not in path
            and all(part not in ('', '.', '..') for part in path.split('/')))


def _members(mapping):
    if not isinstance(mapping, Mapping):
        raise ValueError('invalid_wiki_membership')
    if any(not _valid_path(path) or not _valid_text(digest)
           for path, digest in mapping.items()):
        raise ValueError('invalid_wiki_membership')
    return dict(mapping)


def plan(previous: WikiView | None, observed: Mapping[str, str], *,
         worktree: str, generation: str, raw_generation: str,
         unknown: tuple[str, ...] = (), branch_hint: str | None = None):
    """Build a full snapshot and incremental diff from actual observed files.

    ``unknown`` contains unreadable relative paths or directory prefixes.
    Explicit branch metadata has no effect on membership or visibility.
    """
    if (not _valid_text(worktree) or not _valid_text(generation)
            or not _valid_text(raw_generation)
            or (branch_hint is not None and not _valid_text(branch_hint))
            or not isinstance(unknown, (tuple, list))
            or any(not _valid_path(path) for path in unknown)
            or (previous is not None and
                (not isinstance(previous, WikiView) or previous.worktree != worktree
                 or previous.generation == generation))):
        raise ValueError('invalid_wiki_view_plan')
    current = _members(observed)
    old = dict(previous.members) if previous else {}
    for path, digest in old.items():
        if path not in current and any(path == prefix or path.startswith(prefix + '/')
                                       for prefix in unknown):
            current[path] = digest
    added = tuple(sorted(current.keys() - old.keys()))
    edited = tuple(sorted(path for path in current.keys() & old.keys()
                          if current[path] != old[path]))
    deleted = tuple(sorted(old.keys() - current.keys()))
    view = WikiView(worktree, generation, raw_generation,
                    tuple(sorted(current.items())), branch_hint)
    return MembershipPlan(previous.generation if previous else None,
                          view, added, edited, deleted)


def publish(active: Mapping[str, WikiView], prepared: MembershipPlan):
    """Pure CAS; caller must serialize durable read/write around this decision."""
    if not isinstance(prepared, MembershipPlan) or not isinstance(active, Mapping):
        raise ValueError('invalid_wiki_publication')
    current = active.get(prepared.view.worktree)
    if (current.generation if current else None) != prepared.expected_generation:
        return None
    next_active = dict(active)
    next_active[prepared.view.worktree] = prepared.view
    return next_active


def collectable_hashes(content_hashes, retained_views):
    """Only hashes absent from *all* active and pinned generations may be GC'd."""
    referenced = {digest for view in retained_views for _, digest in view.members}
    return set(content_hashes) - referenced
