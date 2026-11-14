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
