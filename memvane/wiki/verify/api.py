"""Public wiki verification operations."""
from __future__ import annotations


def compile_intent_id(result):
    from memvane.wiki.verify.intent_ack import _compile_intent_id as implementation
    return implementation(result)


def supersede_compile_intent(path):
    from memvane.wiki.verify.intent_ack import _supersede_compile_intent as implementation
    return implementation(path)


def run(root, config, compile_cfg, produced_targets=None):
    from memvane.wiki.verify.wiki_verify_worker import run as implementation
    return implementation(root, config, compile_cfg, produced_targets=produced_targets)


def prepare_queue(root, queue, bound, config=None):
    from memvane.wiki.verify.upgrade_queue import prepare
    return prepare(root, queue, bound, config)


def stage_candidate(path, raw):
    from memvane.wiki.verify.candidate_stage import write
    return write(path, raw)


def pending_name_reserved(queue, rel, *, fresh=False):
    from memvane.wiki.verify.pending_handoff import reserved
    return reserved(queue, rel, fresh=fresh)


def enqueue_replacement(root, config, wiki, queue, job, selected, stage, *, produced_bytes=None):
    from memvane.wiki.verify.pending_handoff import enqueue
    return enqueue(root, config, wiki, queue, job, selected, stage, produced_bytes=produced_bytes)


def pending_revision_target(root, config, revision):
    return pending_source_review(root, config, revision)[0]


def pending_source_review(root, config, revision, *, reuse=True):
    from memvane.wiki.verify.pending_replacement import pending_source_review as implementation
    return implementation(root, config, revision, reuse=reuse)


def verify_skipped_path(root, vcfg):
    from memvane.wiki.verify.wiki_verify_worker import verify_skipped_path as implementation
    return implementation(root, vcfg)


def load_verify_suppressions(path):
    from memvane.wiki.verify.wiki_verify_worker import load_verify_suppressions as implementation
    return implementation(path)


def source_body_hash(text):
    from memvane.wiki.verify.verify_ledger import source_body_hash as implementation
    return implementation(text)


def stamp_verification(path, status, engine, mode, body_hash, source_spans=None):
    from memvane.wiki.verify.verification_stamp import stamp_verification as implementation
    return implementation(path, status, engine, mode, body_hash, source_spans=source_spans)


def create_first_card(root, wiki, queue, job, image, selected, *, review, manifest):
    from memvane.wiki.verify.first_card import create
    return create(root, wiki, queue, job, image, selected, review=review, manifest=manifest)


def native_review_pending(queue):
    from memvane.wiki.verify.native_review import busy
    return busy(queue)

def claim_source_queue(queue, bound, receipt_writer, *, claim=None):
    from memvane.wiki.verify import queue_batch_prepare
    from memvane.wiki.workspace import api as workspace
    try:
        claim = workspace.claim_queue if claim is None else claim
        try:
            queue_batch_prepare.prepare(queue.parents[2], queue, bound)
        except BlockingIOError:
            pass  # An independent native source can still be available.
        return claim(queue, private=True, receipt_writer=receipt_writer,
                                     before_claim=lambda q: queue_batch_prepare.guard_claim(q, bound))
    except BlockingIOError:
        return None

def queue_batch_pending(queue):
    from memvane.wiki.verify.queue_batch_prepare import unresolved
    return unresolved(queue)


def missing_retirement(root, config, target, *, check):
    from memvane.wiki.verify import promotion, wiki_verify_worker as worker
    try:
        return promotion.missing_retirement(
            root, target, check=check,
            repair=lambda row: worker.record_machine_delete(root, config['compile'], row, row['verdict']),
            sync=lambda: worker.reindex_wiki(root, config))
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return 'retry'
