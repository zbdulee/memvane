"""Public wiki workspace operations."""
from __future__ import annotations


def safe_queue_path(*args, **kwargs):
    from memvane.wiki.workspace.queue_paths import _safe_queue_path
    return _safe_queue_path(*args, **kwargs)


def queue_lock_path(*args, **kwargs):
    from memvane.wiki.workspace.queue_paths import _queue_lock_path
    return _queue_lock_path(*args, **kwargs)


def requeue_lines(*args, **kwargs):
    from memvane.wiki.workspace.queue_paths import requeue_lines as implementation
    return implementation(*args, **kwargs)


def now_iso(*args, **kwargs):
    from memvane.wiki.workspace.queue import now_iso as implementation
    return implementation(*args, **kwargs)



def append_jsonl(*args, **kwargs):
    from memvane.wiki.workspace.queue import append_jsonl as implementation
    return implementation(*args, **kwargs)



def read_queue(*args, **kwargs):
    from memvane.wiki.workspace.queue import read_queue as implementation
    return implementation(*args, **kwargs)



def claim_queue(*args, **kwargs):
    from memvane.wiki.workspace.queue import claim_queue as implementation
    return implementation(*args, **kwargs)



def reclaim_orphaned_claimed(*args, **kwargs):
    from memvane.wiki.workspace.queue import reclaim_orphaned_claimed as implementation
    return implementation(*args, **kwargs)


def request_lifecycle(*args, **kwargs):
    from memvane.wiki.workspace.lifecycle_requests import request as implementation
    return implementation(*args, **kwargs)



def claim_lifecycle(*args, **kwargs):
    from memvane.wiki.workspace.lifecycle_requests import claim as implementation
    return implementation(*args, **kwargs)


def selected(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import selected as implementation
    return implementation(*args, **kwargs)


def recheck(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import recheck as implementation
    return implementation(*args, **kwargs)


def request_compile(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import request_compile as implementation
    return implementation(*args, **kwargs)


def request_sync(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import request_sync as implementation
    return implementation(*args, **kwargs)


def route_identity(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import route_identity as implementation
    return implementation(*args, **kwargs)


def schema3_checkout(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import schema3_checkout as implementation
    return implementation(*args, **kwargs)


def queue_busy(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import _queue_busy as implementation
    return implementation(*args, **kwargs)


def deferred_result(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import deferred_result as implementation
    return implementation(*args, **kwargs)
