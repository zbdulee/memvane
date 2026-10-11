"""Prove a source-backed old-to-new card relationship without rewriting output.

The generation receipt and its staged card IDs stay immutable. A rename can
retire an old card only when one new card uniquely preserves every old evidence
quote that is still present in the current source. Changed source quotes may
disappear; unchanged quotes must not silently disappear with their old card.
"""
from __future__ import annotations

import hashlib

import wiki_topical as topical

SCHEMA = 'qmd-topical-replacement-lineage-v1'


def _sources(card):
    return {row['path'] for row in card['sourceRevisions']}


def _anchors(card):
    return {(ev['sourcePath'], ev['quoteAnchor'])
            for claim in card['claims'] for ev in claim['evidence']}


def derive(root, old_cards, new_cards, projection, surviving, snapshot,
           primary_old_id):
    """Return an immutable-recomputable one-to-one replacement proof."""
    old_by_id = {card['cardId']: card for card in old_cards}
    new_by_id = {card['cardId']: card for card in new_cards}
    selected = set(surviving)
    if (not selected or primary_old_id not in old_by_id or
            len(old_by_id) != len(old_cards) or len(new_by_id) != len(new_cards)):
        raise topical.TopicalError('replacement_lineage_unresolved')
    cache = {}
    current = {}
    for path in sorted(selected):
        revision, body, _lines = topical.source_snapshot(root, path, cache)
        if snapshot.get(path, {}).get('sha256') != revision['sha256']:
            raise topical.TopicalError('replacement_source_changed')
        current[path] = body
    affected = [card for card in old_cards
                if projection.get(card['cardId'], {}).get('state') == 'excluded_stale'
                and _sources(card) and
                (_sources(card) <= selected or
                 card['cardId'] == primary_old_id and _sources(card) & selected)]
    if primary_old_id not in {card['cardId'] for card in affected}:
        raise topical.TopicalError('replacement_lineage_unresolved')
    links = []
    used_new = set()
    for old in sorted(affected, key=lambda card: card['cardId']):
        old_id = old['cardId']
        preserved = []
        for path, quote in sorted(anchor for anchor in _anchors(old)
                                  if anchor[0] in selected):
            count = current[path].count(quote)
            if count > 1:
                raise topical.TopicalError('replacement_anchor_ambiguous')
            if count == 1:
                preserved.append((path, quote))
        if old_id in new_by_id:
            new_id = old_id
        else:
            if not preserved:
                if old_id == primary_old_id:
                    raise topical.TopicalError('replacement_lineage_unresolved')
                continue
            matching = [new_id for new_id, new in new_by_id.items()
                        if set(preserved) <= _anchors(new)]
            if not matching and old_id != primary_old_id:
                continue
            if len(matching) != 1:
                raise topical.TopicalError('replacement_lineage_unresolved')
            new_id = matching[0]
        if new_id in used_new or not _sources(new_by_id[new_id]) <= selected:
            raise topical.TopicalError('replacement_lineage_unresolved')
        if not set(preserved) <= _anchors(new_by_id[new_id]):
            raise topical.TopicalError('replacement_lineage_unresolved')
        used_new.add(new_id)
        links.append({'oldCardId': old_id, 'newCardId': new_id,
                      'oldCardSha256': topical.digest(topical.encoded(old)),
                      'newCardSha256': topical.digest(topical.encoded(new_by_id[new_id])),
                      'preservedAnchorsSha256': topical.digest(topical.encoded(
                          [[path, hashlib.sha256(quote.encode()).hexdigest()]
                           for path, quote in preserved]))})
    return {'schema': SCHEMA,
            'sourceSnapshotSha256': topical.digest(topical.encoded(
                [[path, snapshot[path]['sha256']] for path in sorted(selected)])),
            'links': links}
