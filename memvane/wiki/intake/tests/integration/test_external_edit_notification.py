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
            'projectRoot': str(self.root), 'scopeId': 'a' * 32,
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
