"""Read-only candidate-selection port used by recall's live selection.

This module covers only the query-time step that turns recall's already
filtered QMD pool into the model rows a local selector scores, and then maps
the selector's ids back to recall hits. It never captures, enqueues, trains,
writes checkpoints or creates state directories. State resolution
(`seam._private_state`, which may mkdir), the selector itself
(`live_select.choose`) and all diagnostics stay in `recall.py`.

The error codes are a contract: recall maps any ValueError/KeyError/OSError/
UnicodeError from here to its existing `candidate_unavailable` fallback, so
raising the same exception types in the same order keeps public behavior
unchanged.
"""
from __future__ import annotations

QMD_SCHEME = 'qmd://'


def candidate_id(hit):
    """Recall's id for a hit: the daemon `file` without the `qmd://` scheme."""
    return hit['file'].removeprefix(QMD_SCHEME)


def indexed_revisions(corpus):
    """`{(collection, relative): sha256}` from a corpus snapshot, or `{}`."""
    if not corpus:
        return {}
    return {(collection, relative): revision
            for collection, relative, revision in corpus['material']['activeDocuments']}


def pool_rejection(pool, limit, is_wiki_collection):
    """Reason the pool cannot be scored, or None.

    `no_candidates` looks at the whole pool; `non_wiki_pool` only at the
    scored window `pool[:limit]` (same split recall always used).
    """
    if not pool:
        return 'no_candidates'
    if any(not is_wiki_collection(hit.get('_collection', '')) for hit in pool[:limit]):
        return 'non_wiki_pool'
    return None


def candidate_rows(pool, limit, *, resolve_path, snapshot, indexed):
    """Model rows for `pool[:limit]`, all-or-nothing.

    Per hit, in order: resolve the card path, take a bounded snapshot, require
    the snapshot to match the indexed revision, then decode. The first failure
    raises and no partial row list is returned, so the selector never scores a
    subset of the captured pool.
    """
    rows = []
    for hit in pool[:limit]:
        path = resolve_path(hit)
        if path is None:
            raise ValueError('candidate_path_unavailable')
        snapped = snapshot(path)
        if snapped is None:
            raise ValueError('candidate_snapshot_unavailable')
        revision, content = snapped
        collection, separator, relative = candidate_id(hit).partition('/')
        if not separator or indexed.get((collection, relative)) != revision['sha256']:
            raise ValueError('candidate_index_stale')
        rows.append({'id': candidate_id(hit),
                     'revision_sha256': revision['sha256'],
                     'source_text': content.decode('utf8')})
    return rows


def apply_selection(pool, limit, selected_ids):
    """Hits for `selected_ids` in selector order; unknown ids raise KeyError."""
    by_id = {candidate_id(hit): hit for hit in pool[:limit]}
    return [by_id[cid] for cid in selected_ids]
