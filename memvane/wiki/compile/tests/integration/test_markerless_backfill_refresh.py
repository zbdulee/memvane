"""A released refresh may settle the public cohort while the native sibling is outstanding."""
import json
from pathlib import Path

from memvane.settings import api as settings
from memvane.wiki.card import api as freshness
from memvane.wiki.compile.tests.integration import test_backfill_return_controls as controls
from memvane.wiki.compile.tests.integration import test_private_backfill_return as private
from tests.support.case import FixtureCase


class MarkerlessBackfillRefresh(FixtureCase):
    release_tree = controls.BackfillReturnControls.release_tree
    child = controls.BackfillReturnControls.child
    dispatch_frozen_job = controls.BackfillReturnControls.dispatch_frozen_job
    native = private.PrivateBackfillReturn.native

    def test_native_finishes_missing_sibling_after_released_refresh(self):
        for tag, commit in controls.RELEASES.items():
            tree = self.release_tree(tag, commit)
            for pending in (False, True):
                with self.subTest(tag=tag, pending=pending):
                    self.check_refresh(tree, tag, pending)

    def check_refresh(self, tree, tag, pending):
        queue, job, cohort, key, bound = self.dispatch_frozen_job(f'{tag}-refresh-{int(pending)}')
        bound[0].update(state='candidate', generation='g-' + 'd'*32)
        root = Path(bound[0]['projectRoot'])
        config = settings.find_project_config(root)['config']
        config['compile']['semanticDedup'] = {'enabled': False}
        config['compile']['triggers'].append('post_tool_source')
        wiki = root / config['wikiPath'] / 'concepts'
        first, second = wiki / 'first-finding.md', wiki / 'second-finding.md'
        self.assertEqual(self.native(queue, bound, 'after'), [1, ['First finding', 'Second finding']])
        self.assertIn(job['contentRevision'], first.read_text())
        self.assertFalse(second.exists())
        self.assertEqual(queue.read_bytes(), b'')
        self.assertEqual(list(queue.parent.glob(queue.name + '.claimed.*')), [])
        folder, = (queue.parent / '.handoff/v1/backfill').iterdir()
        raw, image = (folder / 'raw').read_bytes(), (folder / 'image').read_bytes()
        self.assertEqual(json.loads(raw), job)
        self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], 'dispatched')

        if pending:
            event = freshness.record_pending_refresh(root, 'one.md', 'codex')
            self.assertIsNotNone(event)
        old = self.child(tree, 'refresh', queue, bound, 0, config=config)
        self.assertEqual(old['processed'], int(pending))
        self.assertEqual(old['extracts'], int(pending))
        if pending:
            settled, = old['settlements']
            self.assertNotIn('initialBackfill', settled)
            self.assertEqual(settled['_processedContentRevision'], job['contentRevision'])
            self.assertEqual(settled['pendingRefresh']['eventId'], event['eventId'])
            self.assertIn(job['contentRevision'], (wiki / 'old-refresh.md').read_text())
        else:
            self.assertEqual(old['settlements'], [])
            self.assertFalse((wiki / 'old-refresh.md').exists())

        self.assertEqual(freshness.unresolved_pending_refreshes(root), [])
        self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], 'settled' if pending else 'dispatched')
        self.assertFalse(second.exists())
        self.assertEqual((folder / 'raw').read_bytes(), raw)
        self.assertEqual((folder / 'image').read_bytes(), image)
        self.assertEqual(self.native(queue, bound, 'recover'), [0, ['Second finding']])
        self.assertIn(job['contentRevision'], second.read_text())
        self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], 'settled')
        self.assertEqual(self.native(queue, bound, 'recover'), [0, []])
