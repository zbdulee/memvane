"""Public wiki deduplication operations."""
from __future__ import annotations

from memvane.wiki.dedup.types import OUTCOME_OK, OUTCOME_UNAVAILABLE, ATTRIBUTION_JOB


def max_pairs_per_compile(compile_cfg):
    from memvane.wiki.dedup.wiki_dedup_judge import max_pairs_per_compile as implementation
    return implementation(compile_cfg)


def is_available(compile_cfg, engine='', attribution=ATTRIBUTION_JOB):
    from memvane.wiki.dedup.wiki_dedup_judge import is_available as implementation
    return implementation(compile_cfg, engine, attribution)


def read_card(path):
    from memvane.wiki.dedup.wiki_dedup_judge import read_card as implementation
    return implementation(path)


def judge_pair(root, compile_cfg, page_a, page_b, engine='', attribution=ATTRIBUTION_JOB):
    from memvane.wiki.dedup.wiki_dedup_judge import judge_pair as implementation
    return implementation(root, compile_cfg, page_a, page_b, engine, attribution)


def diagnostics(info):
    from memvane.wiki.dedup.wiki_dedup_judge import diagnostics as implementation
    return implementation(info)


def run(cwd):
    from memvane.wiki.dedup.wiki_dedup_scan import run as implementation
    return implementation(cwd)


def log(message):
    from memvane.wiki.dedup.wiki_dedup_scan import log as implementation
    return implementation(message)
