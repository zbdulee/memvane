"""Synthetic SQLite proof of pre-limit worktree filtering for Candidate C.

This is deliberately not a QMD adapter or its schema. It shows the required
query and lifetime contract: a complete immutable membership generation is
selected before ranking/limit; a request pins it until release; content is
shared by hash and cannot be reclaimed while any generation references it.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import uuid

import worktree_wiki_membership


def create_schema(db: sqlite3.Connection):
    db.executescript('''
        CREATE TABLE content(hash TEXT PRIMARY KEY, body TEXT NOT NULL,
                             score REAL NOT NULL);
        CREATE TABLE generations(worktree TEXT NOT NULL, generation TEXT NOT NULL,
                                 PRIMARY KEY(worktree, generation));
        CREATE TABLE membership(worktree TEXT NOT NULL, generation TEXT NOT NULL,
                                path TEXT NOT NULL, hash TEXT NOT NULL,
                                PRIMARY KEY(worktree, generation, path));
        CREATE TABLE active(worktree TEXT PRIMARY KEY, generation TEXT NOT NULL);
        CREATE TABLE published(worktree TEXT NOT NULL, generation TEXT NOT NULL,
                               PRIMARY KEY(worktree, generation));
        CREATE TABLE pins(token TEXT PRIMARY KEY, worktree TEXT NOT NULL,
                          generation TEXT NOT NULL);
    ''')


def _identity(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('invalid_view_identity')


def stage(db: sqlite3.Connection, worktree: str, generation: str,
          members: dict[str, str], content: dict[str, tuple[str, float]]):
    """Stage a complete immutable generation, with hash-keyed shared bodies."""
    _identity(worktree)
    _identity(generation)
    if not isinstance(members, dict) or not isinstance(content, dict):
        raise ValueError('invalid_view_membership')
    for path, digest in members.items():
        _identity(path)
        if path.startswith('/') or '\\' in path or any(
                p in ('', '.', '..') for p in path.split('/')):
            raise ValueError('invalid_view_membership')
        _identity(digest)
    for digest, payload in content.items():
        if (not isinstance(payload, tuple) or len(payload) != 2
                or not isinstance(payload[0], str)
                or hashlib.sha256(payload[0].encode()).hexdigest() != digest
                or not isinstance(payload[1], (int, float))
                or not math.isfinite(payload[1])):
            raise ValueError('invalid_content_hash')
    # The caller cannot mutate an already staged generation. Explicit BEGIN
    # also makes a partial stage impossible on errors.
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('SELECT 1 FROM generations WHERE worktree=? AND generation=?',
                      (worktree, generation)).fetchone():
            raise ValueError('generation_already_staged')
        for digest, (body, score) in content.items():
            old = db.execute('SELECT body,score FROM content WHERE hash=?',
                             (digest,)).fetchone()
            if old is not None and old != (body, score):
                raise ValueError('hash_payload_conflict')
            db.execute('INSERT OR IGNORE INTO content VALUES (?,?,?)',
                       (digest, body, score))
        if any(not db.execute('SELECT 1 FROM content WHERE hash=?',
                              (digest,)).fetchone() for digest in members.values()):
            raise ValueError('missing_content')
        db.execute('INSERT INTO generations VALUES (?,?)', (worktree, generation))
        db.executemany('INSERT INTO membership VALUES (?,?,?,?)',
                       ((worktree, generation, path, digest)
                        for path, digest in sorted(members.items())))
        db.commit()
    except Exception:
        db.rollback()
        raise


def publish(db: sqlite3.Connection, worktree: str,
            expected: str | None, generation: str) -> bool:
    """CAS the selected generation; never mutate a published membership."""
    _identity(worktree)
    _identity(generation)
    db.execute('BEGIN IMMEDIATE')
    try:
        if not db.execute('SELECT 1 FROM generations WHERE worktree=? AND generation=?',
                          (worktree, generation)).fetchone():
            raise ValueError('generation_not_staged')
        # A retired predecessor may remain staged for a pinned request. It
        # must never be selected again after a later deletion or branch move.
        if db.execute('SELECT 1 FROM published WHERE worktree=? AND generation=?',
                      (worktree, generation)).fetchone():
            db.rollback()
            return False
        row = db.execute('SELECT generation FROM active WHERE worktree=?',
                         (worktree,)).fetchone()
        if (row[0] if row else None) != expected:
            db.rollback()
            return False
        db.execute('INSERT INTO active(worktree,generation) VALUES (?,?) '
                   'ON CONFLICT(worktree) DO UPDATE SET generation=excluded.generation',
                   (worktree, generation))
        db.execute('INSERT INTO published VALUES (?,?)', (worktree, generation))
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def stage_and_publish(db: sqlite3.Connection,
                      planned: worktree_wiki_membership.MembershipPlan,
                      content: dict[str, tuple[str, float]]) -> bool:
    """Compose a full pure plan with synthetic staging and writer CAS."""
    if not isinstance(planned, worktree_wiki_membership.MembershipPlan):
        raise ValueError('invalid_view_plan')
    view = planned.view
    stage(db, view.worktree, view.generation, dict(view.members), content)
    return publish(db, view.worktree, planned.expected_generation,
                   view.generation)


def pin_request(db: sqlite3.Connection, worktree: str) -> str:
    """Capture and retain the active generation for every phase of a request."""
    _identity(worktree)
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT generation FROM active WHERE worktree=?',
                         (worktree,)).fetchone()
        if row is None:
            raise ValueError('view_not_published')
        token = uuid.uuid4().hex
        db.execute('INSERT INTO pins VALUES (?,?,?)', (token, worktree, row[0]))
        db.commit()
        return token
    except Exception:
        db.rollback()
        raise


def search_pinned(db: sqlite3.Connection, token: str, limit: int):
    """Apply membership in SQL before ORDER BY score and LIMIT."""
    _identity(token)
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid_limit')
    pinned = db.execute('SELECT worktree,generation FROM pins WHERE token=?',
                        (token,)).fetchone()
    if pinned is None:
        raise ValueError('pin_unavailable')
    if not db.execute('SELECT 1 FROM generations WHERE worktree=? AND generation=?',
                      pinned).fetchone():
        raise ValueError('pinned_generation_missing')
    return db.execute('''
        SELECT m.path,m.hash,c.body,c.score FROM membership AS m
        JOIN content AS c ON c.hash=m.hash
        WHERE m.worktree=? AND m.generation=?
        ORDER BY c.score DESC,m.path ASC LIMIT ?
    ''', (*pinned, limit)).fetchall()


def release_request(db: sqlite3.Connection, token: str):
    db.execute('DELETE FROM pins WHERE token=?', (token,))
    db.commit()


def retire(db: sqlite3.Connection, worktree: str, generation: str) -> bool:
    """Retire only an inactive, unpinned generation; its content stays for GC."""
    db.execute('BEGIN IMMEDIATE')
    try:
        if (db.execute('SELECT 1 FROM active WHERE worktree=? AND generation=?',
                       (worktree, generation)).fetchone()
                or db.execute('SELECT 1 FROM pins WHERE worktree=? AND generation=?',
                              (worktree, generation)).fetchone()):
            db.rollback()
            return False
        db.execute('DELETE FROM membership WHERE worktree=? AND generation=?',
                   (worktree, generation))
        db.execute('DELETE FROM generations WHERE worktree=? AND generation=?',
                   (worktree, generation))
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def collect_unreferenced(db: sqlite3.Connection):
    """Only remove hashes with no membership in any retained generation."""
    db.execute('BEGIN IMMEDIATE')
    try:
        rows = db.execute('SELECT hash FROM content WHERE NOT EXISTS '
                          '(SELECT 1 FROM membership WHERE membership.hash=content.hash)'
                          ).fetchall()
        db.executemany('DELETE FROM content WHERE hash=?', rows)
        db.commit()
        return {row[0] for row in rows}
    except Exception:
        db.rollback()
        raise
