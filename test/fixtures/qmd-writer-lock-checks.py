"""Disposable shared writer lock contention and cleanup."""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,'core')
import qmd_writer_lock

with tempfile.TemporaryDirectory(prefix='qmd-writer-lock-') as temp:
    target=Path(temp).resolve()/'qmd-update.lock.d'
    os.environ['QMD_WRITER_LOCKDIR']=str(target)
    with qmd_writer_lock.acquire():
        assert target.is_dir() and (target/'pid').is_file()
        try:
            with qmd_writer_lock.acquire(): pass
        except ValueError as exc:
            assert str(exc)=='qmd_writer_busy'
        else: raise AssertionError('concurrent_writer_accepted')
        assert target.is_dir()
    assert not target.exists()
    target.mkdir()
    (target/'pid').write_text('999999999\n')
    with qmd_writer_lock.acquire():
        assert (target/'pid').read_text().strip()==str(os.getpid())
    assert not target.exists()
print(json.dumps({'busyDenied':True,'released':True,'deadOwnerReclaimed':True}))
