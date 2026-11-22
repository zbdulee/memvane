"""One sync pass's upkeep of the vector arena beside the view store.

The arena (``memvane/vectorcache``) is a derived copy of the store's live
embedding sets that a recall maps instead of reading the store row by row.
The sync owner is its only writer. A pass

    begin           checks the arena against the store, compacts it and
                    catches up a bounded number of sets
    before_publish  appends what a publication is about to make visible
    finish          reports (``vector.cache`` events) and says whether the
                    same wake should be offered again

The wake is offered again only when this pass's catch-up appended at least
one row and sets are still left: an arena that cannot move (quota, a newer
format, an unwritable directory, sets the store cannot read) is ``skipped``
with that reason and waits for the next wake of its own.

Nothing here fails the pass: the arena is derived, the publication goes on.
"""
from __future__ import annotations

from pathlib import Path
import time


def _code(exc):
    return str(getattr(exc, 'reason', None) or type(exc).__name__)[:64]


def _feed(store, spec, blobs=()):
    """``(dimension, live sets, read)`` of the store for the arena's writer (``blobs``: sets about to be live)."""
    from memvane.store import types as store_types
    recipe_id, profile_id = spec['recipe_id'], spec['profile_id']
    dimension, live = store.arena_sets(recipe_id, profile_id, blobs)

    def read(blob):
        try:
            return store.arena_set(blob, recipe_id, profile_id)
        except store_types.BlobStoreError:
            return None  # The writer counts it: a set the store cannot read is not copied.
    return dimension, live, read


def begin(store, path, vectors, blobs=()):
    """Open the pass's writer on the arena beside the store at ``path`` and catch up; never raises."""
    spec = vectors.get('spec')
    if spec is None or vectors.get('arena') is not None:
        return
    from memvane.store import api as store_api
    from memvane.store import types as store_types
    from memvane.vectorcache import api as vectorcache
    session = {'writer': None, 'failed': None, 'started': time.monotonic(), 'path': path}
    try:
        dimension, live, read = _feed(store, spec, blobs)
        session['writer'] = vectorcache.writer(
            Path(path).parent / vectorcache.DIRECTORY, profile_id=spec['profile_id'], recipe_id=spec['recipe_id'],
            dimension=dimension, store_incarnation=store_api.store_identity(store)['incarnation'])
        session['writer'].start(live, read)
    except store_types.BlobStoreError as exc:
        if exc.reason in ('recipe_missing', 'profile_missing'):
            # The spec is registered by this pass's first fill: ``before_publish`` begins then.
            vectors['arenaPath'] = path
            return
        session['failed'] = _code(exc)
    except Exception as exc:  # noqa: BLE001 - a derived file never fails the pass
        session['failed'] = _code(exc)
    vectors['arena'] = session


def before_publish(vectors, store, manifest):
    """Append the sets the arena lacks before the generation of ``manifest`` is published; never raises."""
    session = vectors.get('arena')
    try:
        # Every member's blob: one without a stored set (not eligible, lexical only) names nothing.
        blobs = [member['blob_sha'] for member in manifest['members']]
    except (KeyError, TypeError):
        blobs = []
    if session is None:
        if vectors.get('arenaPath') is not None:
            begin(store, vectors['arenaPath'], vectors, blobs)
        return
    if session['writer'] is None or session['failed']:
        return
    try:
        _dimension, live, read = _feed(store, vectors['spec'], blobs)
        session['writer'].add(live, read)
    except Exception as exc:  # noqa: BLE001 - a derived file never fails the pass
        session['failed'] = _code(exc)


def finish(vectors, decision):
    """Close the pass's writer and emit its ``vector.cache`` events; whether to offer the wake again."""
    session = vectors.pop('arena', None)
    vectors.pop('arenaPath', None)
    if session is None:
        return False
    from memvane.diagnostics import api as diagnostics
    writer, ms = session['writer'], (time.monotonic() - session['started']) * 1000
    try:
        if writer is None:
            diagnostics.emit('vector.cache', outcome='failed', reason=session['failed'], decision=decision, ms=ms)
            return False
        report = writer.summary()
        writer.close()
        common = dict(decision=decision, ms=ms, remaining=report['remaining'], deadRows=report['deadRows'],
                      bytes=report['bytes'])
        for outcome, fields in (('rebuild', {} if report['rebuilt'] else None),
                                ('compact', {} if report['compacted'] else None),
                                ('repair', {'sets': report['repaired']} if report['repaired'] else None),
                                ('catchup', {'rows': report['catchupRows']} if report['catchupRows'] else None),
                                ('append', {'rows': report['appendRows']} if report['appendRows'] else None)):
            if fields is not None:
                diagnostics.emit('vector.cache', outcome=outcome, **common, **fields)
        if report['skipped']:
            diagnostics.emit('vector.cache', outcome='skipped', reason=report['skipped'], **common)
        elif session['failed']:
            diagnostics.emit('vector.cache', outcome='failed', reason=session['failed'], **common)
        return bool(report['catchupRows'] and report['remaining'] and not report['skipped']
                    and not session['failed'])
    except Exception:  # noqa: BLE001 - reporting never fails the pass
        return False
