"""Store lifecycle, identity and read use cases for other domains.

Callers reach these through ``memvane.store.api``. Each function keeps the
store's own checks; callers keep their refusal codes. ``read_header`` opens
the store file itself; callers that compare file identity do so around it.
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


def read_header(path, *, project_root=None, worktree_id=None, recover=False):
    """The store header and worktree rows in one query_only snapshot, or None.

    Opens ``path`` itself: ``kernel.errors._query_only_uri`` (``mode=ro``, or
    ``mode=rw`` where Apple's libsqlite3 needs it), or ``mode=rw`` with
    ``recover=True`` so SQLite rolls back a hot journal a killed writer left
    (only for reads under the queue's sync owner). ``query_only`` and
    ``trusted_schema=OFF`` stay on, so that rollback is the only write. A
    ``mode=ro`` open refuses a hot journal with ``SQLITE_READONLY_ROLLBACK``;
    every ``sqlite3.Error`` reaches the caller untranslated.

    Returns None when the database has no ``store_meta`` row, else a dict
    with ``types.STORE_HEADER_KEYS``: ``worktree`` is the ``worktree_id`` row
    (None when absent or not asked) and ``worktrees`` the ``project_root``
    rows (empty when not asked); each row has ``types.STORE_WORKTREE_KEYS``.
    """
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    from memvane.kernel.errors import _query_only_uri
    from memvane.store.types import STORE_HEADER_KEYS, STORE_WORKTREE_KEYS
    columns = ','.join(STORE_WORKTREE_KEYS)
    uri = Path(path).as_uri() + '?mode=rw' if recover else _query_only_uri(path)
    with closing(sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        meta = db.execute('SELECT incarnation,scope_id,repository_json,settings_policy_digest '
                          'FROM store_meta WHERE singleton=1').fetchone()
        if meta is None:
            return None
        tree = None if worktree_id is None else db.execute(
            f'SELECT {columns} FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
        trees = () if project_root is None else db.execute(
            f'SELECT {columns} FROM worktrees WHERE project_root=?', (project_root,)).fetchall()
    def row(values):
        return dict(zip(STORE_WORKTREE_KEYS, values))
    return dict(zip(STORE_HEADER_KEYS, (*meta, None if tree is None else row(tree),
                                        tuple(row(values) for values in trees))))


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
