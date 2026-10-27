"""Synthetic cross-process compile/verify wiki writer lock contract."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path.cwd() / 'core'))
import wiki_compile as wc
import wiki_mutation_lock as wl

with tempfile.TemporaryDirectory(prefix='qmd-wiki-mutation-lock-') as temporary:
    root = Path(temporary)
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    index = wiki / 'index.md'
    index.write_text('before\n')
    card = wiki / 'card.md'
    card.write_text('---\ntitle: Synthetic\nstatus: generated\n---\nSynthetic body.\n')
    child = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import wiki_compile as wc
Path(sys.argv[3]).write_text('started')
path=Path(sys.argv[2])
if sys.argv[4]=='compile':
 assert wc.write_text_atomic(path,'after\\n')
else:
 assert wc.stamp_verification(path,'verified','synthetic','self',None)
'''
    for action, path, before in (
        ('compile', index, 'before\n'),
        ('verify', card, card.read_text()),
    ):
        marker = root / (action + '.started')
        with wl.lock(root):
            proc = subprocess.Popen([sys.executable, '-c', child,
                str(Path.cwd() / 'core'), str(path), str(marker), action],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists()
            time.sleep(.1)
            assert proc.poll() is None and path.read_text() == before
        out, err = proc.communicate(timeout=5)
        assert proc.returncode == 0, (out, err)
        if action == 'compile': assert path.read_text() == 'after\n'
        else: assert 'status: verified' in path.read_text()
    # A high-level writer may call the atomic helper while already holding the
    # same project lock; this must reuse the local fd rather than self-deadlock.
    with wl.lock(root):
        assert wc.write_text_atomic(path=index, text='nested\n')
        assert wc.stamp_verification(path=card, status='contested', engine='synthetic', mode='self', body_hash=None)
    assert index.read_text() == 'nested\n'
    assert 'status: contested' in card.read_text()
    print(json.dumps({'compileBlocked': True, 'verifyBlocked': True,
                      'nestedLockReentrant': True}))
