"""Public wiki verification intent operations."""
from __future__ import annotations


def compile_intent_id(result):
    from memvane.wiki.verify.intent_ack import _compile_intent_id as implementation
    return implementation(result)


def supersede_compile_intent(path):
    from memvane.wiki.verify.intent_ack import _supersede_compile_intent as implementation
    return implementation(path)
