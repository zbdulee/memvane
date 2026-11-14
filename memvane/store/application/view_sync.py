"""View reads and writes the sync owner's publish pass runs against an open store.

Callers reach these through ``memvane.store.api``. The pass itself (what to
ingest, when to publish, the journal fence) stays in ``memvane.sync``.
"""
from __future__ import annotations

from memvane.store import types as store_types


def read_view_baseline(store, handle, head, *, worktree_id=None, head_ordinal=False):
    """``(worktree row, baseline, head ordinal row)`` read in one store session.

    The worktree row (``SELECT *``) is read only for ``worktree_id`` and the
    ``(ordinal,)`` head row only for ``head_ordinal=True``; otherwise None.
    """
    with store._session() as db:
        tree = (db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
                if worktree_id is not None else None)
        baseline = _rows_baseline(store, db, handle, head)
        ordinal = (db.execute('SELECT ordinal FROM s_heads WHERE view_id=?', (handle.view_id,)).fetchone()
                   if head_ordinal else None)
    return tree, baseline, ordinal


def _rows_baseline(store, db, handle, head):
    """Format 4: the head's open card rows, the view's ready stage, and the head's sources.

    The view keeps one stage. A ready stage over this head is the progress of
    an unfinished change set (``carry``); for a cold view, a ready stage over
    no head is the progress itself (``old``). Members are read in the worker's
    member shape. ``sources`` and ``head_oid`` let an unchanged pass compare
    this pass's observation without reading a stored manifest.
    """
    import json
    from memvane.store.view import view_rows
    from memvane.store.view import view_values
    cold = head['gen_id'] is None

    def member(row):
        return dict(path=row['path'], blob_sha=row['blob_sha'], display_title=row['title'],
                    trust_digest=row['trust_digest'])
    opened = {} if cold else {row['path']: member(row) for row in store._rows_open(db, handle.view_id)}
    staged = {}
    job = db.execute("SELECT job_id,base_gen_id,base_epoch FROM s_stage_jobs WHERE view_id=? AND state='ready'",
                     (handle.view_id,)).fetchone()
    if job is not None and (job[1], job[2]) == (head['gen_id'], head['epoch']):
        staged = dict(opened)
        for item in db.execute('SELECT path,action,input_sha,blob_sha,trust_digest,metadata_json FROM s_stage_items '
                               'WHERE job_id=? ORDER BY path', (job[0],)).fetchall():
            new = view_rows._item_row(view_values.check_stage_item(dict(zip(store_types.STAGE_ITEM_FIELDS,
                                                                            tuple(item)))))
            if new is None:
                staged.pop(item[0], None)
            else:
                staged[item[0]] = member(new)
    spec = head_oid = None
    sources = []
    if not cold:
        generation = db.execute('SELECT g.recipe_id,g.profile_id,g.coverage,g.capture_json FROM s_heads h '
                                'JOIN s_generations g ON g.view_id=h.view_id AND g.ordinal=h.ordinal '
                                'WHERE h.view_id=?', (handle.view_id,)).fetchone()
        if generation[2] == 'complete':
            spec = dict(recipe_id=generation[0], profile_id=generation[1])
        head_oid = json.loads(generation[3])['headOid']
        sources = store._rows_open_sources(db, handle.view_id)
    return dict(old=staged if cold else opened, carry={} if cold else staged, manifest=None,
                vector_spec=spec, head_oid=head_oid, sources=sources)


def head_same(store, head, baseline, observed, handle):
    """The head was published from exactly this observation at the same HEAD."""
    from memvane.store.view import view_values
    if baseline['head_oid'] != handle.head_oid:
        return False
    wanted = []
    for path in sorted(observed, key=lambda item: item.encode('utf8')):
        sha, length, tracked = observed[path]
        content = view_values.source_content(sha, length)
        observation = view_values.source_observation(
            worktree_id=handle.worktree_id, branch_key=handle.branch_key, path=path, tracked_state=tracked,
            binding_state='observed', content_id=content['content_id'], legacy_fingerprint_id=None)
        wanted.append(dict(path=path, observation_id=observation['observation_id']))
    return wanted == baseline['sources']


def compile_evidence_stored(store, handle, intent):
    """Whether a checked compile intent's exact evidence is stored for this view."""
    from memvane.store.blob import blob_values as values
    result = {key: value for key, value in intent.items()
              if key not in ('kind', 'verdict_digest', 'result_digest')}
    if (intent.get('kind') != 'compile' or intent['viewId'] != handle.view_id
            or values.compile_result_digest(result, intent['verdict_digest']) != intent['result_digest']):
        return False
    sources = []
    for fp in intent['sourceFingerprints']['fingerprints']:
        if (fp['worktree_id'], fp['branch_key']) != (handle.worktree_id, handle.branch_key):
            return False
        fingerprint_id = values.digest('memvane-source-fingerprint-v1',
                                       values.fingerprint(fp, store._repository['gitObjectFormat']))
        spans = [dict(span, kind='lines-v1') for span in intent['sourceSpans']
                 if span['path'] == fp['path'] and span['sourceSha256'] == fp['source_sha']]
        sources.append(dict(fingerprint_id=fingerprint_id, spans=spans))
    spec = values.attestation(dict(blob_sha=intent['cardSha'], verification_kind='compiled',
        engine_identity=intent['producingEngine'], policy_digest=handle.policy_digest,
        receipt=dict(schema='memvane-card-evidence-v2', verdict='verified',
                     result_digest=intent['result_digest'], prior_trust=None,
                     verdict_digest=intent['verdict_digest'], verified_mode=intent['verifiedMode'],
                     verify_engine=intent['verifyEngine']), sources=sources))
    try:
        return store.attestation(values.digest('memvane-card-attestation-v1', spec)) == spec
    except store_types.BlobStoreError:
        return False


def linked_worktree_id(store, info, project, deadline, mark_write):
    """The linked worktree's registered id, registering it on first sight.

    ``info`` is the writer's store record (main ``worktree_id``, ``repository``).
    Registration is a store write: ``mark_write()`` (the caller's journal
    fence) runs first. The record is built by ``checkout_view.register_worktree``
    and bound by ``ViewStore.register_worktree`` (no rebinding of an existing row).
    """
    from memvane.kernel.errors import IndexContractError
    from memvane.store.checkout import checkout_view as checkout
    with store._session() as db:
        rows = db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                          (str(project),)).fetchall()
    if len(rows) > 1 or any(row[0] == info['worktree_id'] for row in rows):
        raise IndexContractError('schema3_worktree_rows_invalid')
    if rows:
        return rows[0][0]
    mark_write()
    record = checkout.register_worktree(str(project), info['repository'], deadline=deadline)
    return store.register_worktree(str(project), record)


def embedding_sets_missing(store, blobs, recipe_id, profile_id):
    """Whether any of ``blobs`` lacks the spec's embedding set.

    The recipe and profile rows are checked first (``recipe_missing`` /
    ``profile_missing``); with no blobs that check alone runs.
    """
    with store._session() as db:
        store._recipe(recipe_id); store._profile(profile_id)
        return any(db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                              (blob, recipe_id, profile_id)).fetchone() is None
                   for blob in blobs)


def embedding_coverage(store, targets, recipe_id, profile_id, *, first_from=None):
    """Embedding-set coverage of the fill ``targets`` for one spec (store observation).

    Without ``first_from``: how many targets have the spec's embedding set.
    With ``first_from`` (a target cursor): ``(index, chunked)`` of the first
    target from it without its embedding set, or None.
    """
    if first_from is not None:
        return _first_missing(store, targets, first_from, recipe_id, profile_id)
    return _embedded(store, targets, recipe_id, profile_id)


def _first_missing(store, targets, cursor, recipe_id, profile_id):
    """``(index, chunked)`` of the first target from ``cursor`` without its embedding set, or None."""
    with store._session() as db:
        for index in range(cursor, len(targets)):
            blob = targets[index]['blob_sha']
            if db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                          (blob, recipe_id, profile_id)).fetchone() is None:
                chunked = db.execute('SELECT 1 FROM chunk_sets WHERE blob_sha=? AND recipe_id=?',
                                     (blob, recipe_id)).fetchone()
                return index, chunked is not None
    return None


def _embedded(store, targets, recipe_id, profile_id):
    """How many of ``targets`` have the spec's embedding set (store observation)."""
    with store._session() as db:
        return sum(db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? '
                              'AND profile_id=?', (t['blob_sha'], recipe_id, profile_id)).fetchone() is not None
                   for t in targets)


def fill_plan(store, handle, recipe_id, profile_id):
    """The row view's fill plan: ``(entry, saved, job, targets, stored cursor)``."""
    return store._rows_fill_plan(handle, recipe_id, profile_id)


def put_source_fingerprints(store, handle, scan, observed):
    """Record this scan's observed fingerprints in one write; returns the manifest sources."""
    sources = []
    with store._session(write=True) as db:
        for path in sorted(observed):
            sha, length, tracked = observed[path]
            fingerprint = store._put_fingerprint(db, dict(
                worktree_id=handle.worktree_id, branch_key=handle.branch_key, path=path,
                source_sha=sha, byte_length=length, head_oid=handle.head_oid, tracked_state=tracked,
                binding_state='observed', scan_id=scan['id'], observed_ns=scan['observed_ns']))
            sources.append({'path': path, 'fingerprint_id': fingerprint, 'presence': 'present'})
    return sources
