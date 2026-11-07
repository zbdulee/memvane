"""A real SQLite hot rollback journal for fixtures.

A child process opens the store, spills an uncommitted transaction into the
main file and is killed with SIGKILL. The main file is then torn and its
``-journal`` is hot: the next connection that may write rolls it back, a
``mode=ro`` connection refuses with ``SQLITE_READONLY_ROLLBACK``.
"""
import json
from pathlib import Path
import select
import signal
import subprocess
import sys

WRITER = r'''import json,signal,sqlite3,sys
from pathlib import Path
signal.alarm(25)
db=sqlite3.connect(Path(sys.argv[1]),isolation_level=None)
mode=db.execute('PRAGMA journal_mode').fetchone()[0]
db.execute('PRAGMA cache_size=1')
db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE')
db.execute('CREATE TABLE hot_probe(id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
db.executemany('INSERT INTO hot_probe VALUES(?,?)',[(n,'x'*3000) for n in range(80)])
print(json.dumps(dict(event='uncommitted-spill',journalMode=mode)),flush=True)
sys.stdin.readline()
'''


def kill_mid_write(path):
    """Leave ``path`` torn with a hot ``-journal``; returns ``(bytes before, journal path)``."""
    path = Path(path)
    before = path.read_bytes()
    process = subprocess.Popen([sys.executable, '-B', '-I', '-c', WRITER, str(path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               encoding='utf8')
    try:
        if not select.select([process.stdout], [], [], 15)[0]:
            raise RuntimeError('hot writer checkpoint unavailable')
        evidence = json.loads(process.stdout.readline() or 'null')
        if evidence != dict(event='uncommitted-spill', journalMode='delete'):
            raise RuntimeError('hot writer did not spill a DELETE-mode transaction: %r' % (evidence,))
        process.kill()
        if process.wait(timeout=5) != -signal.SIGKILL:
            raise RuntimeError('hot writer was not killed')
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    journal = Path(str(path) + '-journal')
    if not (journal.is_file() and journal.stat().st_size > 0) or path.read_bytes() == before:
        raise RuntimeError('no hot journal: the pager did not spill into the main file')
    return before, journal
