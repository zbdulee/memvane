"""Public wiki deduplication operations."""
from __future__ import annotations

from memvane.wiki.dedup.types import OUTCOME_OK, OUTCOME_UNAVAILABLE


def max_pairs_per_compile(*args, **kwargs):
    from memvane.wiki.dedup.wiki_dedup_judge import max_pairs_per_compile as implementation
    return implementation(*args, **kwargs)


def is_available(*args, **kwargs):
    from memvane.wiki.dedup.wiki_dedup_judge import is_available as implementation
    return implementation(*args, **kwargs)


def read_card(*args, **kwargs):
    from memvane.wiki.dedup.wiki_dedup_judge import read_card as implementation
    return implementation(*args, **kwargs)


def judge_pair(*args, **kwargs):
    from memvane.wiki.dedup.wiki_dedup_judge import judge_pair as implementation
    return implementation(*args, **kwargs)


def diagnostics(*args, **kwargs):
    from memvane.wiki.dedup.wiki_dedup_judge import diagnostics as implementation
    return implementation(*args, **kwargs)


def run(*args, **kwargs):
    from memvane.wiki.dedup.wiki_dedup_scan import run as implementation
    return implementation(*args, **kwargs)


def log(*args, **kwargs):
    from memvane.wiki.dedup.wiki_dedup_scan import log as implementation
    return implementation(*args, **kwargs)
