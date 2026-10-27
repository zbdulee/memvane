"""A CUD card writer and dedup deletion share the project card lock."""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))
import compile_paths as cp


def hold_writer(root, ready, release):
    with cp.card_write_lock(Path(root)):
        ready.set()
        release.wait(5)


def exercise():
    with tempfile.TemporaryDirectory(prefix='laya-dedup-lock-',
                                     dir=Path(__file__).resolve().parent) as temp:
        root = Path(temp)
        wiki = root / '.auto-context/wiki/entities'
        wiki.mkdir(parents=True)
        (wiki / 'a.md').write_text('# Keeper\n\nA.\n')
        loser = wiki / 'b.md'
        loser.write_text('# Loser\n\nB.\n')
        (root / '.auto-context/settings.json').write_text(json.dumps({
            'indexing': True, 'collections': ['wiki'],
            'collectionPaths': {'wiki': '.auto-context/wiki'},
            'collectionRoles': {'wiki': 'wiki'},
            'wikiPath': '.auto-context/wiki'}))
        compile_dir = root / '.auto-context/compile'
        compile_dir.mkdir()
        (compile_dir / 'dedup-needed.jsonl').write_text(json.dumps({
            'pageA': 'entities/a.md', 'pageB': 'entities/b.md'}) + '\n')
        context = multiprocessing.get_context('spawn')
        ready, release = context.Event(), context.Event()
        holder = context.Process(target=hold_writer, args=(str(root), ready, release))
        holder.start()
        child = None
        try:
            assert ready.wait(5), 'card writer did not acquire lock'
            child = subprocess.Popen([
                sys.executable, '-B', str(ROOT / 'core/wiki_dedup_resolve.py'),
                '--cwd', str(root), '--index', '0', '--action', 'merge',
                '--delete', 'entities/b.md'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, 'QMD_DIRTY_QUEUE': str(root / 'dirty-queue'),
                     'QMD_RECALL_LOG': '', 'QMD_SETUP_GUARD_FIXTURE': '1'})
            time.sleep(0.2)
            assert child.poll() is None, 'dedup ran through an occupied card lock'
            assert loser.exists(), 'card was deleted while another writer owned it'
            release.set()
            holder.join(5)
            assert holder.exitcode == 0
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0, stderr
            assert json.loads(stdout)['action'] == 'deleted'
            assert not loser.exists()
        finally:
            release.set()
            holder.join(5)
            if child is not None and child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)
    print('dedup waits for the shared CUD card-write lock')


if __name__ == '__main__':
    exercise()
