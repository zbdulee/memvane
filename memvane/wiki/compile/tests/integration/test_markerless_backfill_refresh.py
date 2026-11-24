"""A released refresh may settle the public cohort while the native sibling is outstanding."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from memvane.settings import api as settings
from memvane.wiki.card import api as freshness
from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as workspace
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

    def test_markerless_native_claim_resumes_after_old_settlement(self):
        for tag, commit in controls.RELEASES.items():
            with self.subTest(tag=tag):
                self.check_refresh(self.release_tree(tag, commit), tag, True, markerless=True)

    def test_released_permanent_error_preserves_skipped_and_native_completes(self):
        for tag, commit in controls.RELEASES.items():
            tree = self.release_tree(tag, commit)
            for markerless in (False, True):
                with self.subTest(tag=tag, markerless=markerless):
                    self.check_refresh(tree, tag, True, markerless=markerless, invalid=True)

    def test_released_worker_cannot_recover_a_native_edit_or_claim_its_source(self):
        for tag, commit in controls.RELEASES.items():
            with self.subTest(tag=tag):
                tree = self.release_tree(tag, commit)
                queue, _, _, _, bound = self.dispatch_frozen_job(tree, tag + '-edit')
                bound[0].update(state='candidate', generation='g-' + 'd' * 32)
                root = Path(bound[0]['projectRoot'])
                cfg = settings.find_project_config(root)['config']
                cfg['compile']['triggers'].append('post_tool_source')
                cfg['compile']['semanticDedup'] = {'enabled': False}
                # Finish the old frozen job before creating any native edit.
                self.assertEqual(self.child(tree, 'refresh', queue, bound, 0, config=cfg)['processed'], 1)
                source = root / 'one.md'
                with patch.object(settings, 'find_project_config', return_value={'projectRoot': str(root), 'config': cfg}), \
                     patch.object(settings, 'load_project_config', return_value=cfg), \
                     patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'request_compile'):
                    for edit in range(5):
                        source.write_text(f'Updated fact {edit}.')
                        result = intake.enqueue_payload({'cwd': str(root), 'engine': 'codex',
                            'hook_event_name': 'PostToolUse', 'tool_input': {'file_path': str(source)}})
                        self.assertEqual(result['queued'], 1)
                private = workspace.safe_queue_path(root, workspace.rel(queue.name), native=True)
                job, = [json.loads(line) for line in private.read_bytes().splitlines()]
                new, = freshness.unresolved_pending_refreshes_strict(root)
                self.assertEqual(job['pendingRefresh']['eventId'], new['eventId'])
                self.assertEqual(job['source']['path'], 'one.md')
                old = self.child(tree, 'refresh', queue, bound, 0, config=cfg)
                self.assertEqual((old['processed'], old['extracts'], old['settlements']), (0, 0, []))
                prior = self.child(tree, 'record-edit', queue, bound, 0)
                self.assertEqual(freshness.unresolved_pending_refreshes_strict(root), [prior, new])
                self.assertEqual(self.child(tree, 'refresh', queue, bound, 0, config=cfg)['processed'], 1)
                self.assertEqual(freshness.unresolved_pending_refreshes_strict(root), [new])
                self.assertEqual(json.loads(private.read_bytes()), job)

    def check_refresh(self, tree, tag, pending, *, markerless=False, invalid=False):
        queue, job, cohort, key, bound = self.dispatch_frozen_job(tree, f'{tag}-refresh-{int(pending)}-{int(markerless)}')
        if markerless:
            del job['initialBackfill']
            queue.write_text(json.dumps(job) + '\n')
        bound[0].update(state='candidate', generation='g-' + 'd'*32)
        root = Path(bound[0]['projectRoot'])
        config = settings.find_project_config(root)['config']
        config['compile']['semanticDedup'] = {'enabled': False}
        config['compile']['triggers'].append('post_tool_source')
        wiki = root / config['wikiPath'] / 'concepts'
        first, second = wiki / 'first-finding.md', wiki / 'second-finding.md'
        self.assertEqual(self.native(queue, bound, 'after', config=config), [1, ['First finding', 'Second finding']])
        self.assertIn(job['contentRevision'], first.read_text())
        self.assertFalse(second.exists())
        self.assertEqual(queue.read_bytes(), b'')
        self.assertEqual(list(queue.parent.glob(queue.name + '.claimed.*')), [])
        folder, = (queue.parent / '.handoff/v1/backfill').iterdir()
        raw, image = (folder / 'raw').read_bytes(), (folder / 'image').read_bytes()
        self.assertEqual(json.loads(raw), job)
        self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], 'dispatched')

        if pending:
            event = self.child(tree, 'record-edit', queue, bound, 0)
            self.assertIsNotNone(event)
        old = self.child(tree, 'refresh', queue, bound, 0, config=config, invalid=invalid)
        self.assertEqual(old['processed'], int(pending))
        self.assertEqual(old['extracts'], int(pending))
        if pending:
            settled, = old['settlements']
            self.assertNotIn('initialBackfill', settled)
            self.assertEqual(settled['_processedContentRevision'], job['contentRevision'])
            self.assertEqual(settled['pendingRefresh']['eventId'], event['eventId'])
            self.assertEqual(settled.get('_backfillSkipped', False), invalid)
            if invalid:
                audit = [json.loads(line) for line in queue.with_name('candidates.jsonl').read_bytes().splitlines()]
                self.assertTrue(any(row.get('reason') == 'invalid_extractor_json' for row in audit))
                self.assertFalse((wiki / 'old-refresh.md').exists())
            else:
                self.assertIn(job['contentRevision'], (wiki / 'old-refresh.md').read_text())
        else:
            self.assertEqual(old['settlements'], [])
            self.assertFalse((wiki / 'old-refresh.md').exists())

        self.assertEqual([row['eventId'] for row in freshness.unresolved_pending_refreshes(root)],
                         [event['eventId']] if invalid else [])
        terminal = 'skipped' if invalid else 'settled'
        self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], terminal if pending else 'dispatched')
        self.assertFalse(queue.with_name('discard-ledger.jsonl').exists())
        self.assertFalse(second.exists())
        self.assertEqual((folder / 'raw').read_bytes(), raw)
        self.assertEqual((folder / 'image').read_bytes(), image)
        if markerless:
            self.check_no_terminal_grant(queue, job, folder, bound)
        first_run = self.native(queue, bound, 'recover', config=config, trace=True)
        self.assertEqual(first_run, [{'frozen': True, 'event': None, 'extracts': 0, 'writers': ['Second finding']}])
        self.assertIn(job['contentRevision'], second.read_text())
        self.assertEqual(json.loads((folder / 'work').read_bytes())['stage'], 'settled')
        self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], terminal)
        second_run = self.native(queue, bound, 'recover', config=config, trace=True)
        # 1.0.x leaves E1 recoverable. 1.1.0 records a terminal attempt for that event.
        independent = [{'frozen': False, 'event': event['eventId'], 'extracts': 1,
                        'writers': ['First finding', 'Second finding']}] if invalid and tag != 'v1.1.0' else []
        self.assertEqual(second_run, independent)
        self.assertEqual(json.loads(cohort.read_bytes())['items'][key]['stage'], terminal)
        self.assertIn(job['contentRevision'], second.read_text())
        if invalid:
            progress = intake.drain_after_sync(bound[0], intake.observe_grants(bound[0]), recheck=lambda: None,
                                               view_generation='g1', raw_snapshot={key: job['contentRevision']})
            self.assertEqual(progress['initialBackfill']['state'], 'complete_with_skips')

    def check_no_terminal_grant(self, queue, job, folder, bound):
        # A new public occurrence can match every field. A separate private row
        # can match the source SHA and cohort while belonging to another event.
        ready, = queue.parent.glob('.handoff/v1/returns/*/ready-*')
        owner = str(os.getpid()) + '.' + 'f'*32
        records = ((queue.with_name(queue.name + '.claimed.' + owner), job),
                   (ready.with_name('native.claimed.' + owner), dict(job, ts='2099-01-01T00:00:00Z')))
        for claim, candidate in records:
            with claim.open('x') as out:
                print(json.dumps(candidate), file=out)
            try:
                rows = workspace.read_queue(claim)
                with patch.object(workspace, 'recheck'):
                    frozen = intake.reconcile_discards(bound, claimed_rows=rows)
                self.assertEqual(worker._claim_frozen_work(queue, claim, rows, frozen), (claim, {}))
                self.assertEqual(list(folder.parent.iterdir()), [folder])
            finally:
                claim.unlink()
