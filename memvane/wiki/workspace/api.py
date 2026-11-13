"""Public wiki workspace operations."""
from __future__ import annotations


def card_write_lock(*args, **kwargs):
    from memvane.wiki.workspace.compile_paths import card_write_lock as implementation
    return implementation(*args, **kwargs)


def write_text_atomic(*args, **kwargs):
    from memvane.wiki.workspace.atomic_io import write_text_atomic as implementation
    return implementation(*args, **kwargs)


def owner_scope(*args, **kwargs):
    from memvane.wiki.workspace.wiki_selected_laya import _owner_scope as implementation
    return implementation(*args, **kwargs)


def rel(*args, **kwargs):
    from memvane.wiki.workspace.compile_paths import rel as implementation
    return implementation(*args, **kwargs)


def under(*args, **kwargs):
    from memvane.wiki.workspace.compile_paths import under as implementation
    return implementation(*args, **kwargs)


def ledger(*args, **kwargs):
    from memvane.settings.api import ledger as implementation
    return implementation(*args, **kwargs)


def try_compile_run_lock(*args, **kwargs):
    from memvane.wiki.workspace.compile_paths import try_compile_run_lock as implementation
    return implementation(*args, **kwargs)


def validate_compile_run_lock(*args, **kwargs):
    from memvane.wiki.workspace.compile_paths import validate_compile_run_lock as implementation
    return implementation(*args, **kwargs)


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


def state_dir(*args, **kwargs):
    from memvane.wiki.workspace.scan_state import state_dir as implementation
    return implementation(*args, **kwargs)


def project_key(*args, **kwargs):
    from memvane.wiki.workspace.scan_state import project_key as implementation
    return implementation(*args, **kwargs)


def read_state(*args, **kwargs):
    from memvane.wiki.workspace.scan_state import read_state as implementation
    return implementation(*args, **kwargs)


def write_state_atomic(*args, **kwargs):
    from memvane.wiki.workspace.scan_state import write_state_atomic as implementation
    return implementation(*args, **kwargs)
