"""Public wiki card admission operations."""
from __future__ import annotations

from memvane.wiki.card.types import SAFE_KEY_RE, SOURCE_SPANS_KEY, MAX_SOURCE_SPANS, FRONTMATTER_RE


def verified_wiki_row(*args, **kwargs):
    from memvane.wiki.card.trust import _verified_wiki_row
    return _verified_wiki_row(*args, **kwargs)


def verified_wiki_locations(*args, **kwargs):
    from memvane.wiki.card.trust import _verified_wiki_locations
    return _verified_wiki_locations(*args, **kwargs)


def archive_card(*args, **kwargs):
    from memvane.wiki.card.archive import archive_card as implementation
    return implementation(*args, **kwargs)


def frontmatter_sections(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import frontmatter_sections as implementation
    return implementation(*args, **kwargs)


def card_sources_allowed(*args, **kwargs):
    from memvane.wiki.card.trust import card_sources_allowed as implementation
    return implementation(*args, **kwargs)


def card_body_hash(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import card_body_hash as implementation
    return implementation(*args, **kwargs)



def dump(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import dump as implementation
    return implementation(*args, **kwargs)



def dump_flow_mapping(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import dump_flow_mapping as implementation
    return implementation(*args, **kwargs)



def dump_source_revisions(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import dump_source_revisions as implementation
    return implementation(*args, **kwargs)



def dump_source_spans(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import dump_source_spans as implementation
    return implementation(*args, **kwargs)



def fold_inline(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import fold_inline as implementation
    return implementation(*args, **kwargs)



def load(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import load as implementation
    return implementation(*args, **kwargs)



def load_flow_mapping(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import load_flow_mapping as implementation
    return implementation(*args, **kwargs)



def load_source_spans(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import load_source_spans as implementation
    return implementation(*args, **kwargs)



def load_with_issue(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import load_with_issue as implementation
    return implementation(*args, **kwargs)



def normalize_source_revision(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import normalize_source_revision as implementation
    return implementation(*args, **kwargs)



def normalize_source_span(*args, **kwargs):
    from memvane.wiki.card.yaml_scalars import normalize_source_span as implementation
    return implementation(*args, **kwargs)
