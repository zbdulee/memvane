"""Public wiki card admission operations."""
from __future__ import annotations


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
