"""Source-only FTS5 scope experiment on Candidate C's synthetic SQLite store.

This models the 2.5.3 bounded global candidate shape and the later complete
MATCH shape. It is neither QMD's schema nor an operational QMD adapter.
"""
from __future__ import annotations

import sqlite3


def create_fts(db: sqlite3.Connection):
    db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS prototype_fts '
               'USING fts5(hash UNINDEXED, body)')
    db.commit()


def index_staged_content(db: sqlite3.Connection):
    """Reuse one FTS row per physical content hash, across all memberships."""
    db.execute('INSERT INTO prototype_fts(hash,body) '
               'SELECT c.hash,c.body FROM content c WHERE NOT EXISTS '
               '(SELECT 1 FROM prototype_fts f WHERE f.hash=c.hash)')
    db.commit()


def _request(db: sqlite3.Connection, token: str, query: str, limit: int):
    if not isinstance(token, str) or not token or '\x00' in token:
        raise ValueError('invalid_pin')
    if not isinstance(query, str) or not query.strip() or '\x00' in query:
        raise ValueError('invalid_query')
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid_limit')
    row = db.execute('SELECT worktree,generation FROM pins WHERE token=?',
                     (token,)).fetchone()
    if row is None:
        raise ValueError('pin_unavailable')
    if not db.execute('SELECT 1 FROM generations WHERE worktree=? AND generation=?',
                      row).fetchone():
        raise ValueError('pinned_generation_missing')
    return row


def scoped_lex(db: sqlite3.Connection, token: str, query: str, limit: int):
    """Materialize every MATCH, join pinned membership, then rank and limit."""
    worktree, generation = _request(db, token, query, limit)
    return db.execute('''
        WITH matches AS MATERIALIZED (
          SELECT hash, bm25(prototype_fts) AS rank
          FROM prototype_fts WHERE prototype_fts MATCH ?
        )
        SELECT m.path,m.hash,matches.rank FROM matches
        JOIN membership m ON m.hash=matches.hash
        WHERE m.worktree=? AND m.generation=?
        ORDER BY matches.rank ASC,m.path ASC LIMIT ?
    ''', (query, worktree, generation, limit)).fetchall()


def legacy_global_lex(db: sqlite3.Connection, token: str, query: str,
                      limit: int, overfetch: int = 10):
    """Regression comparator only: global top K before pinned scope."""
    worktree, generation = _request(db, token, query, limit)
    if type(overfetch) is not int or overfetch < 1:
        raise ValueError('invalid_overfetch')
    return db.execute('''
        WITH matches AS MATERIALIZED (
          SELECT hash, bm25(prototype_fts) AS rank
          FROM prototype_fts WHERE prototype_fts MATCH ?
          ORDER BY rank ASC LIMIT ?
        )
        SELECT m.path,m.hash,matches.rank FROM matches
        JOIN membership m ON m.hash=matches.hash
        WHERE m.worktree=? AND m.generation=?
        ORDER BY matches.rank ASC,m.path ASC LIMIT ?
    ''', (query, limit * overfetch, worktree, generation, limit)).fetchall()
