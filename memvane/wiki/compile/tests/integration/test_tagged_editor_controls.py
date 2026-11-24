"""Actual released verifiers at the final rename of an independently edited card."""
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.compile.tests.integration import test_pending_handoff as fixtures
from tests.support.case import FixtureCase


TAGS = ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0')


class TaggedEditorControls(FixtureCase):
    prepare = fixtures.PendingHandoff.prepare

    def test_old_writer_and_old_verifier_editor_save_at_last_rename(self):
        repo = Path(__file__).resolve().parents[5]
        for tag in TAGS:
            bundle = fixtures.archived_tag(repo, self.tmp, tag)
            for new, mode in ((True, 'atomic'), (False, 'atomic'), (False, 'inplace')):
                with self.subTest(tag=tag, new=new, mode=mode):
                    self.prepare(tag + '-' + str(new) + '-' + mode)
                    if new:
                        self.target.unlink()
                    env = {**os.environ, 'PYTHONPATH': str(bundle)}
                    subprocess.run([sys.executable, '-B', str(repo / 'memvane/wiki/compile/tests/tagged_writer.py'),
                                    str(bundle), str(self.root), json.dumps(self.config), json.dumps(self.bound),
                                    'single'], cwd=bundle, env=env, capture_output=True, check=True)
                    job = json.loads(self.queue.read_bytes())
                    self.assertEqual('pendingPath' in job, not new)
                    output = subprocess.check_output([sys.executable, '-B',
                        str(repo / 'memvane/wiki/verify/tests/tagged_editor.py'), str(bundle), str(self.root),
                        str(self.target), json.dumps(self.config), mode], cwd=bundle, env=env, text=True)
                    result = json.loads(output)
                    self.assertEqual((result['calls'], result['remaining']), (1, 0))
                    if mode == 'atomic':
                        self.assertEqual((result['links'], result['paths']), (0, []))
                    else:
                        self.assertEqual(result['links'], 1)
                        saved, = result['paths']
                        self.assertEqual(Path(saved).parent, Path('.memvane/compile/retired-wiki'))
