"""Public wiki card admission operations."""
from __future__ import annotations


def verified_wiki_row(*args, **kwargs):
    from memvane.wiki.card.trust import _verified_wiki_row
    return _verified_wiki_row(*args, **kwargs)


def verified_wiki_locations(*args, **kwargs):
    from memvane.wiki.card.trust import _verified_wiki_locations
    return _verified_wiki_locations(*args, **kwargs)
