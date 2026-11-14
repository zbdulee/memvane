"""A dedup merge waits for the project card-write lock another writer holds."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project

REPO = Path(__file__).resolve().parents[5]
# How long the merge is left blocked behind the held lock. A slow start only
# makes the "still blocked" check weaker, never a false failure.
BLOCKED_WINDOW = 0.5


class LockOrder(FixtureCase):
    def test_merge_waits_for_the_shared_card_write_lock(self):
        project = make_project(self.home, git=False)
        entities = project.wiki / 'entities'
        entities.mkdir(parents=True)
        (entities / 'a.md').write_text('# Keeper\n\nA.\n', encoding='utf-8')
        loser = entities / 'b.md'
        loser.write_text('# Loser\n\nB.\n', encoding='utf-8')
        compile_dir = project.root / '.memvane' / 'compile'
        compile_dir.mkdir()
        (compile_dir / 'dedup-needed.jsonl').write_text(
            json.dumps({'pageA': 'entities/a.md', 'pageB': 'entities/b.md'}) + '\n', encoding='utf-8')
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        argv = [sys.executable, '-B', str(REPO / 'entry' / 'run.py'), 'wiki-dedup-resolve',
                '--cwd', str(project.root), '--index', '0', '--action', 'merge', '--delete', 'entities/b.md']
        child = None
        try:
            with workspace.card_write_lock(project.root):
                child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, env=env, cwd=str(project.root))
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=BLOCKED_WINDOW)
                self.assertTrue(loser.exists(), 'deleted while another writer owned the card lock')
            stdout, stderr = child.communicate(timeout=60)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)['action'], 'deleted')
        self.assertFalse(loser.exists())


if __name__ == '__main__':
    unittest.main()
