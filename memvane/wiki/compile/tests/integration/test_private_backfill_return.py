"""A released reaper sees no frozen partial after its owner has published one sibling."""
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.settings import api as settings
from memvane.wiki.compile.tests.integration import test_backfill_return_controls as controls
from tests.support.case import FixtureCase


class PrivateBackfillReturn(FixtureCase):
    dispatch_frozen_job = controls.BackfillReturnControls.dispatch_frozen_job
    release_tree = controls.BackfillReturnControls.release_tree
    child = controls.BackfillReturnControls.child

    def native(self, queue, bound, phase, *, config=None, trace=False):
        if config is None:
            config = settings.find_project_config(bound[0]['projectRoot'])['config']
        config['compile']['semanticDedup'] = {'enabled': False}
        request = dict(queue=str(queue), bound=bound, config=config, phase=phase, trace=trace)
        result = subprocess.run([sys.executable, '-B', str(controls.DRIVER.with_name('native_backfill_return.py'))],
                                input=json.dumps(request), cwd=controls.REPO, text=True, capture_output=True,
                                env={**os.environ, 'PYTHONPATH': str(controls.REPO)})
        self.assertEqual(result.returncode, 0 if phase == 'recover' else 17, result.stderr)
        return json.loads(result.stdout)

    def test_partial_stays_native_across_return_and_old_first_recovery(self):
        for tag, commit in controls.RELEASES.items():
            tree = self.release_tree(tag, commit)
            for phase in ('before', 'after'):
                with self.subTest(tag=tag, phase=phase):
                    queue, job, cohort, key, bound = self.dispatch_frozen_job(tree, tag + phase)
                    bound[0].update(state='candidate', generation='g-' + 'd' * 32)
                    self.assertEqual(self.native(queue, bound, phase), [1, ['First finding', 'Second finding']])
                    self.assertEqual(queue.read_bytes(), b'')
                    self.assertFalse(list(queue.parent.glob(queue.name + '.claimed.*')))
                    folder, = (queue.parent / '.handoff/v1/backfill').iterdir()
                    raw, image = (folder / 'raw').read_bytes(), (folder / 'image').read_bytes()
                    self.assertEqual(json.loads(raw), job)
                    self.assertEqual(self.child(tree, 'consume', queue, bound, 0), [])
                    self.assertEqual((folder / 'raw').read_bytes(), raw)
                    self.assertEqual((folder / 'image').read_bytes(), image)
                    self.assertFalse(queue.with_name('discard-ledger.jsonl').exists())
                    self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], 'dispatched')
                    self.assertEqual(self.native(queue, bound, 'recover'), [0, ['Second finding']])
                    self.assertEqual(self.native(queue, bound, 'recover'), [0, []])
                    self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], 'settled')
                    wiki = Path(bound[0]['projectRoot']) / '.memvane/wiki/concepts'
                    for name in ('first-finding.md', 'second-finding.md'):
                        self.assertIn(job['contentRevision'], (wiki / name).read_text())
