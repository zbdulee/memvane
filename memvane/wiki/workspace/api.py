"""Public wiki workspace operations."""
from __future__ import annotations


def safe_queue_path(*args, **kwargs):
    from memvane.wiki.workspace.queue_paths import _safe_queue_path
    return _safe_queue_path(*args, **kwargs)


def queue_lock_path(*args, **kwargs):
    from memvane.wiki.workspace.queue_paths import _queue_lock_path
    return _queue_lock_path(*args, **kwargs)
