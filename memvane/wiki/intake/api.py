"""Public wiki intake operations."""
from __future__ import annotations

from memvane.wiki.intake.types import (QUEUE_SCAN_MAX_BYTES, SESSION_START_ACCEPTED_TRIGGERS,
                                        GrantUncertain, HOOK_LOCK_SECONDS, POST_TOOL_TRIGGER)


def scan_collections(roots, scope, index, max_files=200, max_bytes=5000000, *, wiki_only=False):
    from memvane.wiki.intake.source_scan import scan_collections as implementation
    return implementation(roots, scope, index, max_files, max_bytes, wiki_only=wiki_only)


def collection_roots(roots):
    from memvane.wiki.intake.source_scan import _collection_roots as implementation
    return implementation(roots)


def schema3_wiki_cards(root):
    """``(sorted card paths, skipped count)`` of the regular ``.md`` cards under a wiki root.

    Dot segments, the root meta pages and links are not cards; a file the
    store would not admit (not owned, linked, over ``MAX_BODY``) is counted in
    ``skipped`` and never read. More than ``MAX_MEMBERS`` cards raises
    ``IndexContractError('schema3_wiki_too_many_cards')``.
    """
    from memvane.wiki.intake.source_scan import schema3_wiki_cards as implementation
    return implementation(root)


def schema3_sources(decision, handle, deadline, *, raw_scan=None):
    """``{project-relative path: (sha256, byte_length, tracked_state)}`` of the raw roots.

    ``tracked_state`` is ``tracked``, ``untracked`` or ``unknown``. A completed
    scan also sets ``raw_scan['snapshot']`` (``{collection/relative: sha256}``);
    a scan over its bounds returns ``{}`` and sets no snapshot.
    """
    from memvane.wiki.intake.source_scan import schema3_sources as implementation
    return implementation(decision, handle, deadline, raw_scan=raw_scan)



def record_grant(decision, kind, *, engine=None, wait=HOOK_LOCK_SECONDS):
    from memvane.wiki.intake.wiki_discovery import record_grant as implementation
    return implementation(decision, kind, engine=engine, wait=wait)



def activate_grant(decision, kind, grant, *, wait=HOOK_LOCK_SECONDS):
    from memvane.wiki.intake.wiki_discovery import activate_grant as implementation
    return implementation(decision, kind, grant, wait=wait)



def revoke_grant(decision, grant, *, wait=HOOK_LOCK_SECONDS):
    from memvane.wiki.intake.wiki_discovery import revoke_grant as implementation
    return implementation(decision, grant, wait=wait)



def observe_grants(decision):
    from memvane.wiki.intake.wiki_discovery import observe_grants as implementation
    return implementation(decision)



def seed_source_snapshot(decision, baseline, *, generation, replace_inert=False):
    from memvane.wiki.intake.wiki_discovery import seed_source_snapshot as implementation
    return implementation(decision, baseline, generation=generation, replace_inert=replace_inert)



def drain_after_sync(decision, observed, *, recheck, view_generation=None, raw_snapshot=None):
    from memvane.wiki.intake.wiki_discovery import drain_after_sync as implementation
    return implementation(decision, observed, recheck=recheck, view_generation=view_generation, raw_snapshot=raw_snapshot)



def enqueue_payload(payload):
    from memvane.wiki.intake.wiki_compile_enqueue import enqueue_payload as implementation
    return implementation(payload)



def enqueue_changed_sources(project_root, config, changed_files, *, session_start=False, initial_backfill=None, engine=None):
    from memvane.wiki.intake.wiki_compile_enqueue import enqueue_changed_sources as implementation
    return implementation(project_root, config, changed_files, session_start=session_start, initial_backfill=initial_backfill, engine=engine)



def source_record(path_value, cwd, project_root, config, engine, trigger=POST_TOOL_TRIGGER):
    from memvane.wiki.intake.wiki_compile_enqueue import _source_record as implementation
    return implementation(path_value, cwd, project_root, config, engine, trigger)



def utc_now():
    from memvane.wiki.intake.wiki_compile_enqueue import _utc_now as implementation
    return implementation()



def queued_revision_keys(path, limit=None):
    from memvane.wiki.intake.wiki_compile_enqueue import _queued_revision_keys as implementation
    return implementation(path, limit)



def append_source_jobs(path, records):
    from memvane.wiki.intake.wiki_compile_enqueue import _append_jsonl as implementation
    return implementation(path, records)



def revision_key(record):
    from memvane.wiki.intake.wiki_source_intent import revision_key as implementation
    return implementation(record)



def settled_job(bound, job):
    from memvane.wiki.intake.wiki_backfill import settled_job as implementation
    return implementation(bound, job)



def settle_job(bound, job):
    from memvane.wiki.intake.wiki_backfill import settle_job as implementation
    return implementation(bound, job)



def reconcile_discards(bound):
    from memvane.wiki.intake.wiki_backfill import reconcile_discards as implementation
    return implementation(bound)



def write_discard_receipt(job):
    from memvane.wiki.intake.wiki_backfill import write_discard_receipt as implementation
    return implementation(job)



def notify(paths, *, cwd, host, origin):
    from memvane.wiki.intake.external_edits import notify as implementation
    return implementation(paths, cwd=cwd, host=host, origin=origin)



def drain(decision, *, recheck, owner=None):
    from memvane.wiki.intake.external_edits import drain as implementation
    return implementation(decision, recheck=recheck, owner=owner)
