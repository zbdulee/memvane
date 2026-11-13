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


def _forward(module, name, *args, **kwargs):
    from importlib import import_module
    return getattr(import_module(module), name)(*args, **kwargs)


class _source_spans:
    """Public source-span parsing and binding operations."""
    is_invalid = staticmethod(lambda value: value == 'invalid')
    bound_to_revisions = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'bound_to_revisions', *a, **k))
    bind_to_card = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'bind_to_card', *a, **k))
    card_revisions = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'card_revisions', *a, **k))
    derive_spans = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'derive_spans', *a, **k))
    evidence_item = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'evidence_item', *a, **k))
    locator = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'locator', *a, **k))
    read_card_spans = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'read_card_spans', *a, **k))
    verify_span = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', 'verify_span', *a, **k))
    _frontmatter_block = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.source_spans', '_frontmatter_block', *a, **k))


class card_support:
    """Public card support operations grouped by their stored contract."""
    source_spans = _source_spans

    class freshness:
        FRESH = 'fresh'
        STALE = 'stale'
        UNKNOWN = 'unknown'
        check_card = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'check_card', *a, **k))
        compare_revision = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'compare_revision', *a, **k))
        latest_unresolved_pending = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'latest_unresolved_pending', *a, **k))
        resolve_pending_refresh = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'resolve_pending_refresh', *a, **k))
        same_revision = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'same_revision', *a, **k))
        snapshot_bytes = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'snapshot_bytes', *a, **k))
        snapshot_file = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'snapshot_file', *a, **k))
        unresolved_pending_refreshes = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'unresolved_pending_refreshes', *a, **k))
        unresolved_pending_refreshes_strict = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'unresolved_pending_refreshes_strict', *a, **k))
        record_pending_refresh = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_freshness', 'record_pending_refresh', *a, **k))

    class source_missing:
        SOURCES_INVALID = 'invalid'
        declared_source_records = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_source_missing', 'declared_source_records', *a, **k))
        wiki_root_of = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_source_missing', 'wiki_root_of', *a, **k))
        allow_roots_of = staticmethod(lambda *a, **k: _forward('memvane.wiki.card.wiki_source_missing', 'allow_roots_of', *a, **k))



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


def _insert_source_revisions(*args, **kwargs):
    from memvane.wiki.card.source_revisions import insert_source_revisions as implementation
    return implementation(*args, **kwargs)
