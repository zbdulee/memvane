"""Resume bounded compatibility inspections of the public manifest."""
import json
import os
import sqlite3
import stat
from contextlib import closing

from memvane.kernel.atomic_rename import private_lock, private_state
from memvane.kernel.errors import _query_only_uri


LIMIT = 8 * 1024 ** 2
_PENDING = 'legacy manifest inspection pending'


def _database(folder, *, create=False):
    for part in ('.handoff', 'v1', 'manifest-scan'):
        folder = folder / part
        if create:
            folder.mkdir(mode=0o700, exist_ok=True)
        elif not os.path.lexists(folder):
            return None
        private_state(folder)
    path = folder / 'projection.db'
    if create:
        os.close(private_lock(path))
    elif not os.path.lexists(path):
        return None
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise OSError('unsafe manifest checkpoint')
    return path


def _cursor(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursor'").fetchone():
        return db.execute('SELECT generation, offset FROM cursor').fetchone()
    return None


def _projection(db):
    # Verify the encoded size in SQLite before allocating Python objects.
    size, = db.execute('SELECT coalesce(sum(length(CAST(row AS BLOB))), 0) FROM projection').fetchone()
    if size > LIMIT:
        raise ValueError('legacy manifest projection limit')
    return size


def _checkpoint(folder, node):
    path = _database(folder)
    if path is None:
        return None, 0, None, None
    with closing(sqlite3.connect(_query_only_uri(path), uri=True, timeout=0.1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        saved = _cursor(db)
        if saved:
            generation, offset = json.loads(saved[0]), saved[1]
            # Old private checkpoints lack a boundary digest. Rebuild from the original.
            if len(generation) == 5 and generation[0] == node[1] and 0 < offset <= generation[1]:
                if generation[:4] == list(node[1:]) and offset == node[2]:
                    _projection(db)
                    return saved, offset, generation[4], [json.loads(row) for row, in db.execute('SELECT row FROM projection')]
                if generation[:4] == list(node[1:]) or generation[1] < node[2]:
                    return saved, offset, generation[4], None
    return saved, 0, None, None


def _save(folder, node, saved, start, offset, proof, rows):
    encoded = [json.dumps(row) for row in rows]
    if sum(len(row) for row in encoded) > LIMIT:
        raise ValueError('legacy manifest projection limit')
    generation = json.dumps([*node[1:], proof])
    with closing(sqlite3.connect(str(_database(folder, create=True)), timeout=0.1)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if _cursor(db) != saved:
            raise ValueError(_PENDING)
        db.execute('CREATE TABLE IF NOT EXISTS cursor (generation TEXT, offset INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS projection (row TEXT PRIMARY KEY) WITHOUT ROWID')
        if start == 0:
            db.execute('DELETE FROM projection')
        db.executemany('INSERT OR IGNORE INTO projection VALUES (?)', ((row,) for row in encoded))
        _projection(db)
        db.execute('DELETE FROM cursor')
        db.execute('INSERT INTO cursor VALUES (?, ?)', (generation, offset))
        if offset == node[2]:
            return [json.loads(row) for row, in db.execute('SELECT row FROM projection')]
    raise ValueError(_PENDING)


def _one_pass(rows):
    if sum(len(json.dumps(row)) for row in rows) > LIMIT:
        raise ValueError('legacy manifest projection limit')
    return rows


def inspect(path, node, read_batch, *, read_only=False):
    """Read the public bytes outside the SQLite write transaction."""
    if node is None:
        return []
    initial = None
    # Most compacted manifests fit in one scan, even when an old DB is locked.
    if node[2] <= 256 * 1024:
        initial = read_batch(path, node, 0, None)
        if initial[1] == node[2]:
            return _one_pass(initial[3])
    try:
        saved, offset, proof, complete = _checkpoint(path.parent, node)
        if complete is not None:
            return complete
    except (OSError, ValueError, sqlite3.Error):
        # A readable one-pass manifest remains usable when the cache is unavailable.
        saved, offset, proof = None, 0, None
    start, end, boundary, rows = (initial if offset == 0 and initial is not None else
                                  read_batch(path, node, offset, proof))
    if start == 0 and end == node[2]:
        return _one_pass(rows)
    if read_only:
        if end != node[2] or start == 0:
            raise ValueError(_PENDING)
        return _finish_readonly(path.parent, saved, rows)
    try:
        return _save(path.parent, node, saved, start, end, boundary, rows)
    except sqlite3.Error as error:
        raise OSError('manifest checkpoint unavailable') from error


def _finish_readonly(folder, saved, rows):
    path = _database(folder)
    if path is None:
        raise ValueError(_PENDING)
    try:
        with closing(sqlite3.connect(_query_only_uri(path), uri=True, timeout=0.1)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if _cursor(db) != saved or _projection(db) + sum(len(json.dumps(row)) for row in rows) > LIMIT:
                raise ValueError(_PENDING)
            return [*(json.loads(row) for row, in db.execute('SELECT row FROM projection')), *rows]
    except sqlite3.Error as error:
        raise OSError('manifest checkpoint unavailable') from error
