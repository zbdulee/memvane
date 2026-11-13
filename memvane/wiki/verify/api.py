"""Public wiki verification operations."""
from __future__ import annotations


def compile_intent_id(*args, **kwargs):
    from memvane.wiki.verify.wiki_verify_worker import _compile_intent_id as implementation
    return implementation(*args, **kwargs)


def load_verify_suppressions(*args, **kwargs):
    from memvane.wiki.verify.wiki_verify_worker import load_verify_suppressions as implementation
    return implementation(*args, **kwargs)


def verify_skipped_path(*args, **kwargs):
    from memvane.wiki.verify.wiki_verify_worker import verify_skipped_path as implementation
    return implementation(*args, **kwargs)


def run(*args, **kwargs):
    from memvane.wiki.verify.wiki_verify_worker import run as implementation
    return implementation(*args, **kwargs)
