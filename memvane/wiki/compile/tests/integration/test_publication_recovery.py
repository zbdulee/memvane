"""Recovery of a prepared verify queue image before and after a released consumer."""
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.compile.tests.integration import test_pending_handoff as cases
from tests.support.case import FixtureCase


class PublicationRecovery(FixtureCase):
    prepare = cases.PendingHandoff.prepare
    write = cases.PendingHandoff.write
    run_tag = cases.PendingHandoff.run_tag
    resume = cases.PendingHandoff.resume

    native_verify = cases.PendingHandoff.native_verify
    finish_after_downgrade = cases.PendingHandoff.finish_after_downgrade

    def child(self, phase, revision=None):
        repo = Path(__file__).resolve().parents[5]
        request = dict(phase=phase, root=str(self.root), config=self.config, bound=self.bound)
        if revision:
            self.source.write_text(revision)
            current = self.project.revision(self.source)
            request['card'] = {'title': 'Decision', 'summary': revision, 'trigger': 'post_tool_source',
                               'engine': 'claude', 'targetPath': self.target.relative_to(self.root).as_posix(),
                               'sourceRevisions': [current],
                               'sources': [{k: current[k] for k in ('kind', 'path', 'collection')}]}
        result = subprocess.run([sys.executable, '-B', str(repo / 'memvane/wiki/compile/tests/publication_child.py')],
                                input=json.dumps(request), cwd=repo, env={**os.environ, 'PYTHONPATH': str(repo)},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if phase == 'recover' else 17, result.stderr)
        if phase == 'recover':
            self.assertIs(json.loads(result.stdout), True)

    def test_first_publication_cuts_preserve_other_names_and_do_not_publish_again(self):
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            self.prepare(tag)
            self.queue.parent.mkdir(exist_ok=True, parents=True)
            decoy = self.queue.parent / '.verify-swap-decoy'
            decoy.write_bytes(b'not a publication')
            for revision in ('First attempted revision.', 'Second attempted revision.'):
                self.child('before', revision)
                swap, = (p for p in self.queue.parent.glob('.verify-swap-*') if p != decoy)
                self.assertEqual(swap.read_bytes(), b'')
                candidate, = self.target.parent.glob('.*.pending-*')
                self.assertTrue(candidate.is_file())
                self.child('recover')
                self.assertFalse(swap.exists())
                self.assertFalse(candidate.exists())

            self.child('after', 'Final published revision.')
            self.finish_after_downgrade(tag)
            published = self.target.read_bytes()
            self.child('recover')
            self.child('recover')
            self.assertEqual(self.queue.read_bytes(), b'')
            self.assertEqual(self.target.read_bytes(), published)
            self.assertEqual(decoy.read_bytes(), b'not a publication')
