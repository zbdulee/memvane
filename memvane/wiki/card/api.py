"""Public wiki card admission operations."""
from __future__ import annotations as _annotations


def bounded_snapshot(*args, **kwargs):
    from memvane.wiki.card.snapshot import bounded_snapshot as implementation
    return implementation(*args, **kwargs)

def parse_frontmatter(*args, **kwargs):
    from memvane.wiki.card.ownership import parse_frontmatter as implementation
    return implementation(*args, **kwargs)


def auto_writable_text(*args, **kwargs):
    from memvane.wiki.card.ownership import auto_writable_text as implementation
    return implementation(*args, **kwargs)


def unresolved_pending_refreshes_strict(*args, **kwargs):
    from memvane.wiki.card.wiki_freshness import unresolved_pending_refreshes_strict as implementation
    return implementation(*args, **kwargs)


def record_pending_refresh(*args, **kwargs):
    from memvane.wiki.card.wiki_freshness import record_pending_refresh as implementation
    return implementation(*args, **kwargs)


def snapshot_bytes(*args, **kwargs):
    from memvane.wiki.card.wiki_freshness import snapshot_bytes as implementation
    return implementation(*args, **kwargs)


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


class source_spans:
    """Public source-span parsing and binding operations."""

    @staticmethod
    def is_invalid(value):
        return value == 'invalid'

    @staticmethod
    def bound_to_revisions(*args, **kwargs):
        from memvane.wiki.card.source_spans import bound_to_revisions as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def bind_to_card(*args, **kwargs):
        from memvane.wiki.card.source_spans import bind_to_card as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def card_revisions(*args, **kwargs):
        from memvane.wiki.card.source_spans import card_revisions as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def derive_spans(*args, **kwargs):
        from memvane.wiki.card.source_spans import derive_spans as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def evidence_item(*args, **kwargs):
        from memvane.wiki.card.source_spans import evidence_item as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def locator(*args, **kwargs):
        from memvane.wiki.card.source_spans import locator as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def read_card_spans(*args, **kwargs):
        from memvane.wiki.card.source_spans import read_card_spans as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def verify_span(*args, **kwargs):
        from memvane.wiki.card.source_spans import verify_span as implementation
        return implementation(*args, **kwargs)

    @staticmethod
    def _frontmatter_block(*args, **kwargs):
        from memvane.wiki.card.source_spans import _frontmatter_block as implementation
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


def extract_card_body(*args, **kwargs):
    from memvane.wiki.card.wiki_card import extract_card_body as implementation
    return implementation(*args, **kwargs)



def frontmatter_source_revisions(*args, **kwargs):
    from memvane.wiki.card.wiki_card import frontmatter_source_revisions as implementation
    return implementation(*args, **kwargs)



def is_auto_trusted_card(*args, **kwargs):
    from memvane.wiki.card.wiki_card import is_auto_trusted_card as implementation
    return implementation(*args, **kwargs)



def normalize_newlines(*args, **kwargs):
    from memvane.wiki.card.wiki_card import normalize_newlines as implementation
    return implementation(*args, **kwargs)



def parse_frontmatter_scalars(*args, **kwargs):
    from memvane.wiki.card.wiki_card import parse_frontmatter_scalars as implementation
    return implementation(*args, **kwargs)



def resolve_existing_source(*args, **kwargs):
    from memvane.wiki.card.wiki_card import resolve_existing_source as implementation
    return implementation(*args, **kwargs)
