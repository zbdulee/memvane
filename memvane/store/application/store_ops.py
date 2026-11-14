"""Store lifecycle, identity and read use cases for other domains.

Callers reach these through ``memvane.store.api``. Each function keeps the
store's own checks; callers keep their refusal codes and their own
connection opening (journal ``_readonly``, query_only connections).
"""
from __future__ import annotations


def observe_binding(path, *, scope_id, repository=None, worktree_id=None, create_root=None, reader=False):
    """Open the view store, observe one worktree, close.

    The store opens as a writer, as a reader with ``reader=True``, or with
    ``create_root`` (a project root) is created: that checkout's repository
    and main worktree are registered first and that worktree is observed.

    Returns the saved worktree row (``saved``), its ``capture``, its checked
    worktree ``record``, the store's ``incarnation``, ``policy_digest`` and
    ``repository``, and ``worktree``: the registered record when created,
    else None. The record is the one the observation itself checked.
    """
    from memvane.store.blob.blob_store import _worktree_record
    from memvane.store.view.view_store import ViewStore
    worktree = None
    if create_root is not None:
        from memvane.store.checkout import checkout_view as checkout
        repository = checkout.register_repository(create_root)
        worktree = checkout.register_worktree(create_root, repository)
        store = ViewStore.create(path, project_root=create_root, repository=repository,
                                 worktree=worktree, scope_id=scope_id)
        worktree_id = worktree['worktreeId']
    elif reader:
        store = ViewStore.open_reader(path, repository=repository, scope_id=scope_id)
    else:
        store = ViewStore.open(path, repository=repository, scope_id=scope_id)
    try:
        saved, capture = store._observe_worktree(worktree_id)
        return {'saved': saved, 'capture': capture,
                'record': _worktree_record(saved, store._repository, store._policy),
                'incarnation': store._incarnation, 'policy_digest': store._policy,
                'repository': store._repository, 'worktree': worktree}
    finally:
        store.close()


def open_store(path, *, repository, scope_id, reader=False, deadline=None):
    """An open view store; the caller closes it. ``deadline`` applies to a reader open only."""
    from memvane.store.view.view_store import ViewStore
    if reader:
        return ViewStore.open_reader(path, repository=repository, scope_id=scope_id, deadline=deadline)
    if deadline is not None:
        raise TypeError('open_store deadline requires reader=True')
    return ViewStore.open(path, repository=repository, scope_id=scope_id)


def store_identity(store, handle=None):
    """``incarnation``, ``policy_digest`` and ``repository`` of an open store.

    With ``handle`` (issued by this store's ``capture_view``) also its ``capture``.
    """
    identity = {'incarnation': store._incarnation, 'policy_digest': store._policy,
                'repository': store._repository}
    if handle is not None:
        identity['capture'] = store._handles[handle.token]['capture']
    return identity


def stored_header(db, *, identity=False):
    """The store header row read on the caller's connection.

    ``(repository_json,)``; with ``identity=True``
    ``(incarnation, scope_id, repository_json, settings_policy_digest)``.
    """
    if identity:
        return db.execute('SELECT incarnation,scope_id,repository_json,settings_policy_digest '
                          'FROM store_meta WHERE singleton=1').fetchone()
    return db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()


def stored_worktrees(db, *, project_root=None, worktree_id=None, records=False):
    """Worktree rows read on the caller's connection.

    By ``worktree_id``: one ``(project_root, binding_digest)`` row or None.
    By ``project_root``: every ``(worktree_id,)`` row, or with ``records=True``
    every ``(record_json,)`` row.
    """
    if worktree_id is not None:
        return db.execute('SELECT project_root,binding_digest FROM worktrees WHERE worktree_id=?',
                          (worktree_id,)).fetchone()
    if records:
        return db.execute('SELECT record_json FROM worktrees WHERE project_root=?',
                          (project_root,)).fetchall()
    return db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                      (project_root,)).fetchall()


def pinned_vector(store, pin):
    """The vector spec of a pinned generation, or None for a lexical generation."""
    return store._pinned(pin)['vector']


def vector_profile(store, profile_id, *, session):
    """The stored vector profile; ``session=True`` reads it in its own store session."""
    if session:
        with store._session():
            return store._profile(profile_id)
    return store._profile(profile_id)


def eligible_members(store, gen_id, *, deadline, limit):
    """``[(path, blob_sha)]`` of a generation's eligible cards in binary path order."""
    with store._reading(deadline):
        return store._eligible_members(gen_id, limit=limit)


def worktree_wiki_root(store, worktree_id, *, deadline):
    """``(wiki_root,)`` of a registered worktree row, or None, read under ``deadline``."""
    with store._reading(deadline) as db:
        return db.execute('SELECT wiki_root FROM worktrees WHERE worktree_id=?',
                          (worktree_id,)).fetchone()


def admitted_paths(saved, found):
    """Card paths of ``found`` that the bound worktree's current ``skipPaths`` do not cover."""
    from memvane.store.view.view_store import _skip_patterns
    from memvane.store.view.view_values import skipped
    skips = _skip_patterns(saved)
    return [p for p in found if not skipped(p, skips)]
