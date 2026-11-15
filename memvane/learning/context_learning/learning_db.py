"""Read an existing private learning database."""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from memvane.kernel.errors import _query_only_uri


@contextmanager
def read_database(path, *, timeout=.02):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError('unsafe_learning_database')
    with closing(sqlite3.connect(_query_only_uri(path), uri=True, timeout=timeout)) as db:
        db.execute('PRAGMA query_only=ON')
        yield db
