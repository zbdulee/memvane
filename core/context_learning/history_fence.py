"""SQLite write fence for legacy history files during a prepared cutover.

Old installed plugin processes do not take the new cooperative flock. SQLite
triggers make their ordinary INSERT/UPDATE/DELETE statements fail even when
they opened a connection before the cutover. The original tables stay intact.
"""
from __future__ import annotations

from contextlib import contextmanager, closing, ExitStack
import hashlib
from pathlib import Path
import sqlite3


PREFIX = 'qmd_history_fence_'
OPERATIONS = ('INSERT', 'UPDATE', 'DELETE')


def _name(table, operation):
    token = hashlib.sha256(table.encode()).hexdigest()[:16]
    return f'{PREFIX}{token}_{operation.lower()}'


def _sql(table, operation):
    if not table.replace('_', '').isalnum():
        raise ValueError('history_fence_table_name_invalid')
    quoted = '"' + table.replace('"', '""') + '"'
    return (f'CREATE TRIGGER "{_name(table, operation)}" BEFORE {operation} '
            f'ON {quoted} BEGIN SELECT RAISE(ABORT, \'qmd_history_fenced\'); END')


def _expected(tables):
    return {_name(table, op): _sql(table, op)
            for table in sorted(tables) for op in OPERATIONS}


def _actual(db):
    return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                           "AND name LIKE ?", (PREFIX + '%',)))


def verify(db, tables):
    if _actual(db) != _expected(tables):
        raise ValueError('history_legacy_fence_changed')


def install(db, tables):
    if _actual(db):
        raise ValueError('history_legacy_fence_preexisting')
    for sql in _expected(tables).values():
        db.execute(sql)
    verify(db, tables)


def remove(db, tables):
    verify(db, tables)
    for name in _expected(tables):
        db.execute('DROP TRIGGER "' + name + '"')
    if _actual(db):
        raise ValueError('history_legacy_fence_removal_failed')


def remove_partial(db, tables):
    """Unwind a cutover interrupted before pointer publication."""
    actual = _actual(db)
    if not actual:
        return
    if actual != _expected(tables):
        raise ValueError('history_legacy_fence_partial_or_changed')
    remove(db, tables)


@contextmanager
def exclusive_pair(paths, *, timeout=5):
    """Hold SQLite writer locks on both sources in shared-then-private order."""
    with ExitStack() as stack:
        databases = []
        for path in paths:
            uri = Path(path).as_uri() + '?mode=rw'
            db = stack.enter_context(closing(sqlite3.connect(uri, uri=True,
                                                             timeout=timeout)))
            db.execute(f'PRAGMA busy_timeout={max(1, int(timeout * 1000))}')
            db.execute('BEGIN EXCLUSIVE')
            databases.append(db)
        try:
            yield tuple(databases)
        except BaseException:
            for db in databases:
                db.rollback()
            raise
        else:
            for db in databases:
                db.commit()
