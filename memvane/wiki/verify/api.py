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


def enqueue_review(root, job, image=None, *, generation=None):
    from memvane.wiki.verify.review_queue import enqueue_review as implementation
    return implementation(root, job, image, generation=generation)


def publish_first_card(root, wiki_root, job, image, *, review, manifest):
    from memvane.wiki.verify.review_queue import publish_first_card as implementation
    return implementation(root, wiki_root, job, image, review=review, manifest=manifest)


def pending_source_review(root, config, revision, *, reuse=True):
    from memvane.wiki.verify.review_queue import pending_source_review as implementation
    return implementation(root, config, revision, reuse=reuse)


def source_body_hash(text):
    from memvane.wiki.verify.verify_ledger import source_body_hash as implementation
    return implementation(text)


def stamp_verification(path, status, engine, mode, body_hash, source_spans=None):
    from memvane.wiki.verify.verification_stamp import stamp_verification as implementation
    return implementation(path, status, engine, mode, body_hash, source_spans=source_spans)



def v1_reviews(root, config):
    from memvane.wiki.verify.review_queue import v1_reviews as implementation
    return implementation(root, config)


def reload_unreviewed(root, config):
    from memvane.wiki.verify.review_queue import reload_unreviewed as implementation
    return implementation(root, config)
