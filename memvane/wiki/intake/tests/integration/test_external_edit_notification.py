"""External notification writes bounded hints through the owner's proof boundary."""
import hashlib
from unittest.mock import patch

from memvane.journal import api as journal
from memvane.runtime import api as runtime
from memvane.wiki.intake import external_edits as external
from tests.support.case import FixtureCase
from tests.support.project import make_project


class ExternalEditNotification(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, name='owner', git=False)
        self.root = self.project.root
        self.state = self.home / 'owner-state'
        self.state.mkdir(mode=0o700)
        self.decision = {
            'projectRoot': str(self.root), 'installId': 'd' * 32, 'scopeId': 'a' * 32,
            'storeIncarnation': 'b' * 32, 'storePath': str(self.state / 'store.sqlite'),
            'collections': [
                {'name': self.project.raw_name, 'role': 'raw', 'sourceRoot': str(self.root)},
                {'name': self.project.wiki_name, 'role': 'wiki', 'sourceRoot': str(self.project.wiki)},
            ],
            'collectionRoots': {self.project.raw_name: str(self.root),
                                self.project.wiki_name: str(self.project.wiki)},
            'recallSettings': {'skipPaths': ['build']},
        }
        self.target = {'projectRoot': str(self.root), 'rawRoot': str(self.root),
                       'decision': self.decision}
        self.source = self.project.write_note('docs/note.md', 'first\n')
        self.proof = {'notificationOnly': True}
        self.targets = patch.object(external, '_targets', return_value=[self.target])
        self.targets.start()
        self.addCleanup(self.targets.stop)
        self.proof_patch = patch.object(journal, 'verify_notification_target', return_value=self.proof)
        self.proof_mock = self.proof_patch.start()
        self.addCleanup(self.proof_patch.stop)
        self.wake_patch = patch.object(runtime, 'request', return_value={'status': 'queued'})
        self.wake = self.wake_patch.start()
        self.addCleanup(self.wake_patch.stop)

    def notify(self, *paths, host='codex'):
        return external.notify(paths, cwd=self.home, host=host, origin=None)

    def box(self):
        return external._read_box(external._inbox_path(self.decision), self.decision)

    def test_edit_writes_blob_hint_and_wakes_only_owner(self):
        self.assertEqual(self.notify(self.source), {'owners': 1, 'failed': 0, 'overflow': 0})
        data = self.source.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        self.assertEqual(self.box()['edits'], [{'path': 'docs/note.md', 'blob': blob, 'host': 'codex'}])
        self.wake.assert_called_once_with('laya-sync', str(self.root))
        self.assertEqual(self.proof_mock.call_count, 2)

    def test_same_blob_deduplicates_and_new_blob_replaces_pending_hint(self):
        self.notify(self.source)
        first = self.box()['edits'][0]
        self.notify(self.source)
        self.assertEqual(self.box()['edits'], [first])
        self.source.write_text('second\n')
        self.notify(self.source, host='claude')
        latest = self.box()['edits']
        self.assertEqual(len(latest), 1)
        self.assertNotEqual(latest[0]['blob'], first['blob'])
        self.assertEqual(latest[0]['host'], 'claude')

    def test_deleted_source_is_notified(self):
        self.source.unlink()
        self.assertEqual(self.notify(self.source)['owners'], 1)
        self.assertEqual(self.box()['edits'][0]['blob'], 'deleted')

    def test_changed_owner_proof_refuses_hint_and_wake(self):
        self.proof_mock.side_effect = [self.proof, {'notificationOnly': True, 'changed': True},
                                       {'notificationOnly': True, 'changed': True}]
        self.assertEqual(self.notify(self.source), {'owners': 0, 'failed': 1, 'overflow': 0})
        self.assertEqual(self.box()['edits'], [])
        self.wake.assert_not_called()

    def test_unsafe_or_excluded_sources_do_not_route(self):
        outside = self.home / 'outside'
        outside.mkdir()
        (self.root / 'link').symlink_to(outside, target_is_directory=True)
        result = self.notify(self.root / 'link/escape.md', self.root / '.memvane/wiki/x.md',
                             self.root / 'build/skip.md')
        self.assertEqual(result, {'owners': 0, 'failed': 0, 'overflow': 0})
        self.assertEqual(self.box()['edits'], [])
        self.proof_mock.assert_not_called()
        self.wake.assert_not_called()

    def test_origin_host_is_preserved_and_inbox_keeps_no_source_body(self):
        for host in ('claude', 'codex', 'hermes'):
            with self.subTest(host=host):
                self.source.write_text('secret marker ' + host + '\n')
                self.assertEqual(self.notify(self.source, host=host)['owners'], 1)
                inbox = external._inbox_path(self.decision)
                self.assertEqual(self.box()['edits'][0]['host'], host)
                self.assertNotIn('secret marker', inbox.read_text())
                self.assertEqual(inbox.stat().st_mode & 0o777, 0o600)

    def test_inbox_cap_preserves_existing_rows_and_overflow_still_wakes(self):
        sources = [self.project.write_note('docs/n%d.md' % n, 'note %d\n' % n)
                   for n in range(3)]
        with patch.object(external, 'MAX_INBOX_EDITS', 2):
            for path in sources[:2]:
                self.assertEqual(self.notify(path)['owners'], 1)
            before = self.box()['edits']
            self.wake.reset_mock()
            result = self.notify(sources[2])
            self.assertEqual(result, {'owners': 1, 'failed': 0, 'overflow': 1})
            self.assertEqual(self.box()['edits'], before)
            self.wake.assert_called_once_with('laya-sync', str(self.root))

    def test_backslash_filename_cannot_poison_owner_inbox(self):
        unsupported = self.project.write_note('docs/a\\\\b.md', 'not a portable path\n')
        self.assertEqual(self.notify(unsupported)['failed'], 1)
        self.assertEqual(self.notify(self.source)['owners'], 1)
        self.assertEqual([row['path'] for row in self.box()['edits']], ['docs/note.md'])

    def test_symlinked_inbox_does_not_modify_target(self):
        inbox = external._inbox_path(self.decision)
        outside = self.home / 'outside.json'
        outside.write_text('{}')
        inbox.symlink_to(outside)
        self.assertEqual(self.notify(self.source)['failed'], 1)
        self.assertEqual(outside.read_text(), '{}')

    def drain(self, recheck=lambda: None):
        from memvane.wiki.workspace import api as workspace
        from tests.support.settings_fake import expanded, use_config
        use_config(self, self.project, expanded(self.project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_tool_source'],
            'extractor': {'builtins': ['claude', 'codex']}}))
        with patch.object(journal, 'verify_selected_route_for_write',
                          return_value={'productRouteAllowed': True}), \
             patch.object(journal, 'select_route', return_value=self.decision), \
             patch.object(workspace, 'selected', return_value=(self.decision, {'productRouteAllowed': True})), \
             patch.object(workspace, 'request_compile'):
            return external.drain(self.decision, recheck=recheck)

    def queue_rows(self):
        import json
        from memvane.wiki.workspace import api as workspace, types as workspace_types
        queue = self.root / workspace.rel(workspace_types.SOURCE_QUEUE)
        return [json.loads(line) for line in queue.read_text().splitlines()] if queue.exists() else []

    def test_drain_repeats_full_proof_and_compiles_current_blob(self):
        self.notify(self.source)
        result = self.drain()
        self.assertEqual((result['processed'], result['remaining']), (1, 0))
        self.assertEqual([(row['source']['path'], row['engine']) for row in self.queue_rows()],
                         [('docs/note.md', 'codex')])
        self.drain()
        self.assertEqual(len(self.queue_rows()), 1)

    def test_stale_blob_is_not_compiled(self):
        self.notify(self.source)
        self.source.write_text('different bytes\n')
        self.drain()
        self.assertEqual(self.queue_rows(), [])

    def test_stale_deleted_hint_is_not_compiled(self):
        self.source.unlink()
        self.notify(self.source)
        self.source.write_text('restored\n')
        self.drain()
        self.assertEqual(self.queue_rows(), [])

    def test_current_deletion_requests_existing_owner_lifecycle(self):
        from memvane.wiki.workspace import api as workspace
        self.source.unlink()
        self.notify(self.source)
        with patch.object(workspace, 'request_lifecycle') as request:
            self.drain()
        request.assert_called_once_with(self.decision, 'source_edit')
        self.assertEqual(self.queue_rows(), [])

    def test_changed_proof_during_drain_keeps_hint(self):
        self.notify(self.source)
        def fail_recheck():
            raise ValueError('selected_proof_changed')
        with self.assertRaisesRegex(ValueError, 'selected_proof_changed'):
            self.drain(recheck=fail_recheck)
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(len(self.box()['edits']), 1)

    def test_inbox_symlink_is_refused_by_drain(self):
        self.notify(self.source)
        inbox = external._inbox_path(self.decision)
        inbox.unlink()
        outside = self.home / 'outside.json'
        outside.write_text('{}')
        inbox.symlink_to(outside)
        from memvane.wiki.intake.types import DiscoveryBlocked
        with self.assertRaisesRegex(DiscoveryBlocked, 'state_unreadable'):
            self.drain()
        self.assertEqual(outside.read_text(), '{}')
        self.assertEqual(self.queue_rows(), [])

    def test_only_current_of_several_blobs_compiles(self):
        self.notify(self.source)
        self.source.write_text('new content\n')
        self.notify(self.source)
        self.drain()
        self.assertEqual([row['source']['path'] for row in self.queue_rows()], ['docs/note.md'])

    def test_stale_uncompiled_hint_can_be_notified_again_when_current(self):
        original = self.source.read_bytes()
        self.notify(self.source)
        self.source.write_text('unobserved content\n')
        self.drain()
        self.assertEqual(self.queue_rows(), [])
        self.source.write_bytes(original)
        self.notify(self.source)
        self.assertEqual([row['path'] for row in self.box()['edits']], ['docs/note.md'])

    def test_current_skip_paths_apply_at_drain(self):
        self.notify(self.source)
        self.decision['recallSettings']['skipPaths'] = ['docs']
        self.drain()
        self.assertEqual(self.queue_rows(), [])

    def test_new_hint_during_owner_hash_is_not_lost(self):
        self.notify(self.source)
        revision = external._revision
        armed = [True]
        def interleave(path):
            result = revision(path)
            if path == self.source and armed[0]:
                armed[0] = False
                self.source.write_text('new bytes during owner hashing\n')
                self.notify(self.source)
            return result
        with patch.object(external, '_revision', side_effect=interleave):
            self.drain()
        current = external._revision(self.source)[0]
        self.assertEqual([row['blob'] for row in self.box()['edits']], [current])
        self.assertEqual(self.queue_rows(), [])
