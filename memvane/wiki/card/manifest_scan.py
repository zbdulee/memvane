"""Resume compatibility inspection of one large manifest generation across processes."""
import json
import os
import sqlite3
from contextlib import closing

from memvane.kernel.atomic_rename import private_lock, private_state


def _database(folder):
    for part in ('.handoff', 'v1', 'manifest-scan'):
        folder = folder / part
        folder.mkdir(mode=0o700, exist_ok=True)
        private_state(folder)
    path = folder / 'projection.db'
    fd = private_lock(path)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise OSError('unsafe manifest checkpoint')
    finally:
        os.close(fd)
    return path


def inspect(path, node, read_batch):
    """A transaction commits the next LF boundary and its distinct identities together."""
    if node is None:
        return []
    initial = None
    checkpoint = path.parent / '.handoff/v1/manifest-scan/projection.db'
    if not os.path.lexists(checkpoint):
        initial = read_batch(path, node, 0)
        if initial[0] == node[2]:
            return initial[1]

    # The device number can change across boots. The descriptor check in read_batch
    # still compares the current device and inode with the public name on each pass.
    generation = json.dumps(node[1:])
    try:
        with closing(sqlite3.connect(str(_database(path.parent)), timeout=0)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS cursor (generation TEXT, offset INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS projection (row TEXT PRIMARY KEY) WITHOUT ROWID')
            cursor = db.execute('SELECT generation, offset FROM cursor').fetchone()
            if cursor is None or cursor[0] != generation:
                db.execute('DELETE FROM projection')
                db.execute('DELETE FROM cursor')
                db.execute('INSERT INTO cursor VALUES (?, 0)', (generation,))
                offset = 0
            else:
                offset = cursor[1]
            if not 0 <= offset <= node[2]:
                raise ValueError('invalid manifest cursor')
            if offset < node[2]:
                offset, rows = initial if offset == 0 and initial else read_batch(path, node, offset)
                db.executemany('INSERT OR IGNORE INTO projection VALUES (?)',
                               ((json.dumps(row),) for row in rows))
                db.execute('UPDATE cursor SET offset = ?', (offset,))
            if offset == node[2]:
                return [json.loads(row) for row, in db.execute('SELECT row FROM projection')]
    except sqlite3.Error as error:
        raise OSError('manifest checkpoint unavailable') from error
    raise ValueError('legacy manifest inspection pending')
