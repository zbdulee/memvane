"""Public wiki card admission operations.

Card freshness states and the invalid markers live in ``types.py``.
"""
from __future__ import annotations as _annotations


# Card text and frontmatter.
def parse_frontmatter(text):
    from memvane.wiki.card.ownership import parse_frontmatter as implementation
    return implementation(text)

def render_frontmatter_fields(text, updates, blocks=None):
    from memvane.wiki.card.frontmatter_render import _render_frontmatter_fields as implementation
    return implementation(text, updates, blocks)

def auto_writable_text(text):
    from memvane.wiki.card.ownership import auto_writable_text as implementation
    return implementation(text)

def extract_card_body(text, body_start):
    from memvane.wiki.card.wiki_card import extract_card_body as implementation
    return implementation(text, body_start)

def normalize_newlines(text):
    from memvane.wiki.card.wiki_card import normalize_newlines as implementation
    return implementation(text)

def parse_frontmatter_scalars(block, issues=None):
    from memvane.wiki.card.wiki_card import parse_frontmatter_scalars as implementation
    return implementation(block, issues)

def frontmatter_source_revisions(block):
    from memvane.wiki.card.wiki_card import frontmatter_source_revisions as implementation
    return implementation(block)

def is_auto_trusted_card(meta):
    from memvane.wiki.card.wiki_card import is_auto_trusted_card as implementation
    return implementation(meta)

def resolve_existing_source(raw_path, project_root, allow_roots):
    from memvane.wiki.card.wiki_card import resolve_existing_source as implementation
    return implementation(raw_path, project_root, allow_roots)

def insert_source_revisions(path, revisions):
    from memvane.wiki.card.source_revisions import insert_source_revisions as implementation
    return implementation(path, revisions)

def archive_card(root, card, text, proof_hash):
    from memvane.wiki.card.archive import archive_card as implementation
    return implementation(root, card, text, proof_hash)

def bounded_snapshot(path):
    from memvane.wiki.card.snapshot import bounded_snapshot as implementation
    return implementation(path)


# YAML scalars.
def frontmatter_sections(frontmatter):
    from memvane.wiki.card.yaml_scalars import frontmatter_sections as implementation
    return implementation(frontmatter)

def card_body_hash(text):
    from memvane.wiki.card.yaml_scalars import card_body_hash as implementation
    return implementation(text)

def dump(value):
    from memvane.wiki.card.yaml_scalars import dump as implementation
    return implementation(value)

def dump_flow_mapping(mapping):
    from memvane.wiki.card.yaml_scalars import dump_flow_mapping as implementation
    return implementation(mapping)

def dump_source_revisions(revisions):
    from memvane.wiki.card.yaml_scalars import dump_source_revisions as implementation
    return implementation(revisions)

def dump_source_spans(spans):
    from memvane.wiki.card.yaml_scalars import dump_source_spans as implementation
    return implementation(spans)

def fold_inline(value):
    from memvane.wiki.card.yaml_scalars import fold_inline as implementation
    return implementation(value)

def load(raw):
    from memvane.wiki.card.yaml_scalars import load as implementation
    return implementation(raw)

def load_flow_mapping(raw):
    from memvane.wiki.card.yaml_scalars import load_flow_mapping as implementation
    return implementation(raw)

def load_source_spans(entries):
    from memvane.wiki.card.yaml_scalars import load_source_spans as implementation
    return implementation(entries)

def load_with_issue(raw):
    from memvane.wiki.card.yaml_scalars import load_with_issue as implementation
    return implementation(raw)

def normalize_source_revision(record):
    from memvane.wiki.card.yaml_scalars import normalize_source_revision as implementation
    return implementation(record)

def normalize_source_span(record):
    from memvane.wiki.card.yaml_scalars import normalize_source_span as implementation
    return implementation(record)


# Recall trust.
def verified_wiki_row(row, decision, pending_sources=frozenset(), source_locations=None, source_skip_paths=(), *, current_source=True, reasons=None):
    from memvane.wiki.card.trust import _verified_wiki_row as implementation
    return implementation(row, decision, pending_sources, source_locations, source_skip_paths, current_source=current_source, reasons=reasons)

def verified_wiki_locations(row, decision, pending_sources, source_skip_paths=(), *, current_source=True, reasons=None):
    from memvane.wiki.card.trust import _verified_wiki_locations as implementation
    return implementation(row, decision, pending_sources, source_skip_paths, current_source=current_source, reasons=reasons)

def card_sources_allowed(body, skip_paths):
    from memvane.wiki.card.trust import card_sources_allowed as implementation
    return implementation(body, skip_paths)


# Declared sources.
def declared_source_records(block):
    from memvane.wiki.card.wiki_source_missing import declared_source_records as implementation
    return implementation(block)

def wiki_root_of(root, config):
    from memvane.wiki.card.wiki_source_missing import wiki_root_of as implementation
    return implementation(root, config)

def allow_roots_of(config):
    from memvane.wiki.card.wiki_source_missing import allow_roots_of as implementation
    return implementation(config)


# Source freshness and pending refreshes.
def check_card(card, project_root, allow_roots):
    from memvane.wiki.card.wiki_freshness import check_card as implementation
    return implementation(card, project_root, allow_roots)

def compare_revision(path, expected):
    from memvane.wiki.card.wiki_freshness import compare_revision as implementation
    return implementation(path, expected)

def same_revision(left, right):
    from memvane.wiki.card.wiki_freshness import same_revision as implementation
    return implementation(left, right)

def snapshot_bytes(path):
    from memvane.wiki.card.wiki_freshness import snapshot_bytes as implementation
    return implementation(path)

def snapshot_file(path):
    from memvane.wiki.card.wiki_freshness import snapshot_file as implementation
    return implementation(path)

def record_pending_refresh(root, source_path, engine):
    from memvane.wiki.card.wiki_freshness import record_pending_refresh as implementation
    return implementation(root, source_path, engine)

def resolve_pending_refresh(root, pending, captured_revision, compiled_revisions, compiled_event_id=None):
    from memvane.wiki.card.wiki_freshness import resolve_pending_refresh as implementation
    return implementation(root, pending, captured_revision, compiled_revisions, compiled_event_id)

def latest_unresolved_pending(root, source_path):
    from memvane.wiki.card.wiki_freshness import latest_unresolved_pending as implementation
    return implementation(root, source_path)

def unresolved_pending_refreshes(root):
    from memvane.wiki.card.wiki_freshness import unresolved_pending_refreshes as implementation
    return implementation(root)

def unresolved_pending_refreshes_strict(root):
    from memvane.wiki.card.wiki_freshness import unresolved_pending_refreshes_strict as implementation
    return implementation(root)


# Source spans.
def frontmatter_block(text):
    from memvane.wiki.card.source_spans import _frontmatter_block as implementation
    return implementation(text)

def read_card_spans(block):
    from memvane.wiki.card.source_spans import read_card_spans as implementation
    return implementation(block)

def card_revisions(text):
    from memvane.wiki.card.source_spans import card_revisions as implementation
    return implementation(text)

def derive_spans(claims, evidence):
    from memvane.wiki.card.source_spans import derive_spans as implementation
    return implementation(claims, evidence)

def evidence_item(path, sha256, data, shown_chars):
    from memvane.wiki.card.source_spans import evidence_item as implementation
    return implementation(path, sha256, data, shown_chars)

def bind_to_card(spans, text):
    from memvane.wiki.card.source_spans import bind_to_card as implementation
    return implementation(spans, text)

def bound_to_revisions(spans, revisions):
    from memvane.wiki.card.source_spans import bound_to_revisions as implementation
    return implementation(spans, revisions)

def locator(span):
    from memvane.wiki.card.source_spans import locator as implementation
    return implementation(span)
