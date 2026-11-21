"""Git children of one prompt recall, for store records sealed under another boot's device number.

Two fixture projects are onboarded in one HOME. The second registers its
repository and worktree while ``checkout_view._directory`` reports ``dev + 7``:
the immutable store records then differ from every later live observation in
the device number only, which is what an APFS volume renumbered at boot leaves
behind. ``hooks/run-hook recall`` then runs once per project with a counting
``git`` first on ``PATH``. The assertion is the number of git children, never
a wall clock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.case import FixtureCase  # noqa: E402
from tests.support.install_builder import build_install  # noqa: E402
from tests.support.project import make_project, settle_index  # noqa: E402

HOOK_TIMEOUT = 60
SANDBOX_KEYS = ('QMD_SANDBOX', 'CODEX_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'GEMINI_SANDBOX')
DEVICE_SHIFT = 7


@unittest.skipUnless(sys.platform == 'darwin', 'the checkout observation cache needs APFS timestamps')
class CheckoutCacheEndToEnd(FixtureCase):
    def onboard(self, index, token):
        project = make_project(self.home, name='cache%d' % index, dirname='project%d' % index)
        note = project.write_note('notes/' + token + '.md', 'Synthetic source for ' + token + '.\n')
        project.write_card('concepts/' + token + '.md', title=token + ' card',
                           body=token + ' verified body\n', sources=[note])
        project.commit('fixture')
        install = build_install(self, project, install_id='%x' % (index + 4) * 32)
        settle_index(project.root)
        return project, install

    def counting_git(self):
        """A ``git`` that appends one line per invocation, in a directory to put first on PATH."""
        real = shutil.which('git')
        self.assertIsNotNone(real)
        folder = self.home / 'gitbin'
        folder.mkdir(mode=0o700)
        self.count_file = self.home / 'git-children'
        shim = folder / 'git'
        shim.write_text('#!/bin/sh\nprintf "x\\n" >> %s\nexec %s "$@"\n' % (self.count_file, real),
                        encoding='ascii')
        shim.chmod(0o700)
        return folder

    def git_children(self, folder, project, prompt, install):
        self.count_file.write_text('', encoding='ascii')
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
               'PATH': str(folder) + os.pathsep + os.environ.get('PATH', '')}
        for key in SANDBOX_KEYS:
            env.pop(key, None)
        seen = len([e for e in install.events() if e.get('event') == 'recall'])
        payload = {'session_id': 'e2e-cache', 'cwd': str(project.root),
                   'hook_event_name': 'UserPromptSubmit', 'prompt': prompt}
        result = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), 'recall', 'claude'],
                                input=json.dumps(payload), capture_output=True, text=True,
                                timeout=HOOK_TIMEOUT, env=env, cwd=str(project.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        recalls = [e for e in install.events() if e.get('event') == 'recall']
        self.assertEqual(len(recalls), seen + 1, recalls[seen:])
        self.assertEqual(recalls[-1].get('outcome'), 'served', recalls[-1])
        return len(self.count_file.read_text(encoding='ascii').splitlines())

    def test_one_recall_spawns_the_same_git_children_after_the_device_moved(self):
        from memvane.store.checkout import checkout_view
        same_project, same_install = self.onboard(0, 'kestrelprobe')
        real = checkout_view._directory
        def moved(path):
            value = real(path)
            return {**value, 'dev': value['dev'] + DEVICE_SHIFT}
        with mock.patch.object(checkout_view, '_directory', side_effect=moved):
            moved_project, moved_install = self.onboard(1, 'ospreyprobe')
        with moved_install.db() as db:
            repository = json.loads(db.execute('SELECT repository_json FROM store_meta').fetchone()[0])
            records = [json.loads(row[0]) for row in db.execute('SELECT record_json FROM worktrees')]
        live = os.stat(moved_project.root).st_dev
        self.assertEqual(repository['commonGitDir']['dev'], live + DEVICE_SHIFT)
        self.assertEqual([record['project']['dev'] for record in records], [live + DEVICE_SHIFT])
        folder = self.counting_git()
        # Each count is of the second recall: the first one of a process tree may warm tool lookups.
        baseline = [self.git_children(folder, same_project, 'kestrelprobe', same_install) for _ in range(2)]
        observed = [self.git_children(folder, moved_project, 'ospreyprobe', moved_install) for _ in range(2)]
        print(json.dumps({'gitChildren': {'sameDevice': baseline, 'movedDevice': observed}}), file=sys.stderr)
        self.assertGreater(baseline[1], 0)
        self.assertEqual(observed[1], baseline[1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
