"""Public wiki intake operations."""
from __future__ import annotations

from memvane.wiki.intake.types import (RAW_SCAN_MAX_FILES, RAW_SCAN_MAX_BYTES, QUEUE_SCAN_MAX_BYTES,
                                        SESSION_START_ACCEPTED_TRIGGERS, GrantUncertain)


def scan_collections(*args, **kwargs):
    from memvane.wiki.intake.source_scan import scan_collections as implementation
    return implementation(*args, **kwargs)


def collection_roots(*args, **kwargs):
    from memvane.wiki.intake.source_scan import _collection_roots as implementation
    return implementation(*args, **kwargs)



def record_grant(*args, **kwargs):
    from memvane.wiki.intake.wiki_discovery import record_grant as implementation
    return implementation(*args, **kwargs)



def activate_grant(*args, **kwargs):
    from memvane.wiki.intake.wiki_discovery import activate_grant as implementation
    return implementation(*args, **kwargs)



def revoke_grant(*args, **kwargs):
    from memvane.wiki.intake.wiki_discovery import revoke_grant as implementation
    return implementation(*args, **kwargs)



def observe_grants(*args, **kwargs):
    from memvane.wiki.intake.wiki_discovery import observe_grants as implementation
    return implementation(*args, **kwargs)



def seed_source_snapshot(*args, **kwargs):
    from memvane.wiki.intake.wiki_discovery import seed_source_snapshot as implementation
    return implementation(*args, **kwargs)



def drain_after_sync(*args, **kwargs):
    from memvane.wiki.intake.wiki_discovery import drain_after_sync as implementation
    return implementation(*args, **kwargs)



def enqueue_payload(*args, **kwargs):
    from memvane.wiki.intake.wiki_compile_enqueue import enqueue_payload as implementation
    return implementation(*args, **kwargs)



def enqueue_changed_sources(*args, **kwargs):
    from memvane.wiki.intake.wiki_compile_enqueue import enqueue_changed_sources as implementation
    return implementation(*args, **kwargs)



def source_record(*args, **kwargs):
    from memvane.wiki.intake.wiki_compile_enqueue import _source_record as implementation
    return implementation(*args, **kwargs)



def utc_now(*args, **kwargs):
    from memvane.wiki.intake.wiki_compile_enqueue import _utc_now as implementation
    return implementation(*args, **kwargs)



def queued_revision_keys(*args, **kwargs):
    from memvane.wiki.intake.wiki_compile_enqueue import _queued_revision_keys as implementation
    return implementation(*args, **kwargs)



def append_source_jobs(*args, **kwargs):
    from memvane.wiki.intake.wiki_compile_enqueue import _append_jsonl as implementation
    return implementation(*args, **kwargs)



def revision_key(*args, **kwargs):
    from memvane.wiki.intake.wiki_source_intent import revision_key as implementation
    return implementation(*args, **kwargs)



def settled_job(*args, **kwargs):
    from memvane.wiki.intake.wiki_backfill import settled_job as implementation
    return implementation(*args, **kwargs)



def settle_job(*args, **kwargs):
    from memvane.wiki.intake.wiki_backfill import settle_job as implementation
    return implementation(*args, **kwargs)



def reconcile_discards(*args, **kwargs):
    from memvane.wiki.intake.wiki_backfill import reconcile_discards as implementation
    return implementation(*args, **kwargs)



def write_discard_receipt(*args, **kwargs):
    from memvane.wiki.intake.wiki_backfill import write_discard_receipt as implementation
    return implementation(*args, **kwargs)



def notify(*args, **kwargs):
    from memvane.wiki.intake.external_edits import notify as implementation
    return implementation(*args, **kwargs)



def drain(*args, **kwargs):
    from memvane.wiki.intake.external_edits import drain as implementation
    return implementation(*args, **kwargs)
