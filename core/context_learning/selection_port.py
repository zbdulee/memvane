"""Read-only candidate-selection port used by recall's live selection.

This module covers only the query-time step that turns recall's already
filtered QMD pool into the model rows a local selector scores, and then maps
the selector's ids back to recall hits. It never captures, enqueues, trains,
writes checkpoints or creates state directories, and stays pure. State
resolution (`seam._private_state`, which may mkdir) and the selector itself
(`live_select.choose`) are called by the `selection_read` coordinator; all
diagnostics stay in `recall.py`.

The error codes are a contract: `selection_read` maps any ValueError/
KeyError/OSError/UnicodeError/TypeError from here to the existing
`candidate_unavailable` fallback, so raising the same exception types in the
same order keeps public behavior unchanged.
"""
from __future__ import annotations

PUBLISHED_LIMIT = 15

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


def published_candidate_rows(pool, wiki_names, scope_id):
    """Model input from already verified, published Laya rows; no source reads.

    The selected index query owns provenance and generation verification. This
    port checks its immutable row contract again before passing any card bytes
    to the existing local selector. A partial or mixed invalid pool is refused.
    """
    import hashlib
    import re
    sha256 = re.compile(r'[0-9a-f]{64}\Z')
    generation = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
    if (not isinstance(pool, list) or len(pool) > PUBLISHED_LIMIT
            or not isinstance(wiki_names, (tuple, list, frozenset))
            or not wiki_names or any(not isinstance(n, str) or not n for n in wiki_names)):
        raise ValueError('published_pool_invalid')
    names = set(wiki_names)
    seen = set()
    result = []
    for row in pool:
        if not isinstance(row, dict):
            raise ValueError('published_candidate_invalid')
        path, digest, body = (row.get('path'), row.get('hash'), row.get('body'))
        if (not isinstance(path, str) or path in seen
                or not isinstance(digest, str) or not sha256.fullmatch(digest)
                or not isinstance(body, str)
                or not isinstance(row.get('generation'), str)
                or not generation.fullmatch(row['generation'])
                or row.get('scope') != scope_id):
            raise ValueError('published_candidate_invalid')
        collection, sep, relative = path.partition('/')
        if (not sep or collection not in names or not relative
                or any(part in ('', '.', '..') for part in relative.split('/'))
                or hashlib.sha256(body.encode('utf-8')).hexdigest() != digest):
            raise ValueError('published_candidate_invalid')
        seen.add(path)
        result.append({'id': path, 'revision_sha256': digest, 'source_text': body})
    return result


def apply_published_selection(pool, selected_ids):
    """Map the model's IDs back to the exact proven rows, in model order."""
    if (not isinstance(selected_ids, list) or len(selected_ids) > 3
            or len(selected_ids) != len(set(selected_ids))):
        raise ValueError('published_selection_invalid')
    by_id = {row['path']: row for row in pool}
    if any(not isinstance(candidate, str) or candidate not in by_id
           for candidate in selected_ids):
        raise ValueError('published_selection_invalid')
    return [by_id[candidate] for candidate in selected_ids]
