"""Public wiki workspace operations."""
from __future__ import annotations as _annotations

from memvane.wiki.workspace.types import ACTIVITY_TAIL_CHARS, LOG_MAX_BYTES, UNRESOLVED_BINDING


def card_write_lock(root, *, folder=None):
    from memvane.wiki.workspace.compile_paths import card_write_lock as implementation
    return implementation(root, folder=folder)


def write_text_atomic(path, text):
    from memvane.wiki.workspace.atomic_io import write_text_atomic as implementation
    return implementation(path, text)


def owner_scope(bound, owner):
    from memvane.wiki.workspace.wiki_selected_laya import _owner_scope as implementation
    return implementation(bound, owner)


def rel(name, root=None):
    from memvane.wiki.workspace.compile_paths import rel as implementation
    return implementation(name, root)


def under(root, name):
    from memvane.wiki.workspace.compile_paths import under as implementation
    return implementation(root, name)


def try_compile_run_lock(root, *, folder=None):
    from memvane.wiki.workspace.compile_paths import try_compile_run_lock as implementation
    return implementation(root, folder=folder)


def validate_compile_run_lock(root, fd):
    from memvane.wiki.workspace.compile_paths import validate_compile_run_lock as implementation
    return implementation(root, fd)


def safe_queue_path(project_root, configured_path=None):
    from memvane.wiki.workspace.queue_paths import _safe_queue_path
    return _safe_queue_path(project_root, configured_path)


def queue_lock_path(path):
    from memvane.wiki.workspace.queue_paths import _queue_lock_path
    return _queue_lock_path(path)


def requeue_lines(path, raw_lines):
    from memvane.wiki.workspace.queue_paths import requeue_lines as implementation
    return implementation(path, raw_lines)


def append_jsonl(path, payload):
    from memvane.wiki.workspace.queue import append_jsonl as implementation
    return implementation(path, payload)


def read_queue(path):
    from memvane.wiki.workspace.queue import read_queue as implementation
    return implementation(path)


def claim_queue(path, *, receipt_writer=None):
    from memvane.wiki.workspace.queue import claim_queue as implementation
    return implementation(path, receipt_writer=receipt_writer)


def request_lifecycle(decision, trigger):
    from memvane.wiki.workspace.lifecycle_requests import request as implementation
    return implementation(decision, trigger)


def claim_lifecycle(root):
    from memvane.wiki.workspace.lifecycle_requests import claim as implementation
    return implementation(root)


def selected(root):
    from memvane.wiki.workspace.wiki_selected_laya import selected as implementation
    return implementation(root)


def recheck(bound, *, owner=None):
    from memvane.wiki.workspace.wiki_selected_laya import recheck as implementation
    return implementation(bound, owner=owner)


def request_compile(root, bound, *, owner=None):
    from memvane.wiki.workspace.wiki_selected_laya import request_compile as implementation
    return implementation(root, bound, owner=owner)


def request_sync(root, bound=None):
    from memvane.wiki.workspace.wiki_selected_laya import request_sync as implementation
    return implementation(root, bound)


def route_identity(bound):
    from memvane.wiki.workspace.wiki_selected_laya import route_identity as implementation
    return implementation(bound)


def schema3_checkout(bound):
    from memvane.wiki.workspace.wiki_selected_laya import schema3_checkout as implementation
    return implementation(bound)


def queue_busy(queue, *, receipt_writer=None):
    from memvane.wiki.workspace.wiki_selected_laya import _queue_busy as implementation
    return implementation(queue, receipt_writer=receipt_writer)


def deferred_result(exc):
    from memvane.wiki.workspace.wiki_selected_laya import deferred_result as implementation
    return implementation(exc)


def state_dir(explicit=None):
    from memvane.wiki.workspace.scan_state import state_dir as implementation
    return implementation(explicit)


def project_key(project_root, config_path):
    from memvane.wiki.workspace.scan_state import project_key as implementation
    return implementation(project_root, config_path)


def read_state(path):
    from memvane.wiki.workspace.scan_state import read_state as implementation
    return implementation(path)


def write_state_atomic(path, snapshot):
    from memvane.wiki.workspace.scan_state import write_state_atomic as implementation
    return implementation(path, snapshot)


def trim_jsonl(path, max_bytes=LOG_MAX_BYTES):
    from memvane.wiki.workspace.atomic_io import trim_jsonl as implementation
    return implementation(path, max_bytes)


def mutation_lock(root):
    from memvane.wiki.workspace.wiki_mutation_lock import lock as implementation
    return implementation(root)


def guard_path(func):
    from memvane.wiki.workspace.wiki_mutation_lock import guard_path as implementation
    return implementation(func)


def append_activity(root, action, rel, title, bound=UNRESOLVED_BINDING, *, reason=None):
    from memvane.wiki.workspace.wiki_activity_log import append as implementation
    return implementation(root, action, rel, title, bound, reason=reason)


def activity_tail(root, bound=UNRESOLVED_BINDING, limit=ACTIVITY_TAIL_CHARS):
    from memvane.wiki.workspace.wiki_activity_log import tail as implementation
    return implementation(root, bound, limit)
