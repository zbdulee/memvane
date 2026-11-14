"""Source discovery journals changed revisions until an accepted grant drains them."""
import hashlib
from unittest.mock import patch

from memvane.wiki.intake import wiki_discovery as discovery
from memvane.wiki.workspace import api as workspace, types as workspace_types
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class DiscoveryCursor(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.source = self.project.write_note('docs/note.md', 'initial\n')
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        self.journal = state / 'journal.sqlite'
        self.decision = {'scopeId': 'a' * 32, 'storeIncarnation': 'b' * 32,
                         'installId': 'c' * 32, 'storePath': str(state / 'store.sqlite'),
                         'journalPath': str(self.journal), 'projectRoot': str(self.root),
                         'collections': [
                             {'name': self.project.raw_name, 'role': 'raw', 'sourceRoot': str(self.root)},
                             {'name': self.project.wiki_name, 'role': 'wiki', 'sourceRoot': str(self.project.wiki)}],
                         'collectionRoots': {self.project.raw_name: str(self.root),
                                             self.project.wiki_name: str(self.project.wiki)}}
        self.key = self.project.raw_name + '/docs/note.md'
        self.initial = hashlib.sha256(self.source.read_bytes()).hexdigest()
        use_config(self, self.project, expanded(self.project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['codex']}}))

    def drain(self, generation, snapshot):
        observed = discovery.observe_grants(self.decision)
        with patch.object(workspace, 'selected', return_value=None):
            return discovery.drain_after_sync(self.decision, observed, recheck=lambda: None,
                                              view_generation=generation, raw_snapshot=snapshot)

    def accept(self):
        grant = discovery.record_grant(self.decision, 'sync', engine='codex')
        self.assertTrue(discovery.activate_grant(self.decision, 'sync', grant))
        return grant

    def test_ungranted_change_waits_until_accepted_grant(self):
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.source.write_text('changed\n')
        changed = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.drain('g2', {self.key: changed})
        self.assertEqual((result['state'], result['reason'], result['pending']),
                         ('pending', 'no_accepted_grant', 1))
        self.accept()
        result = self.drain('g2', {self.key: changed})
        self.assertEqual((result['state'], result['queued'], result['pending']), ('drained', 1, 0))
        queue = self.root / workspace.rel(workspace_types.SOURCE_QUEUE)
        self.assertIn('docs/note.md', queue.read_text())
        self.assertIn(changed, queue.read_text())
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [])

    def test_corrupt_cursor_fails_closed_without_reset(self):
        path = discovery._paths(self.decision)[0]
        path.write_bytes(b'{bad')
        path.chmod(0o600)
        self.accept()
        result = self.drain('g2', {self.key: self.initial})
        self.assertEqual((result['state'], result['reason']), ('blocked', 'state_corrupt'))
        self.assertEqual(path.read_bytes(), b'{bad')
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [])

    def test_missing_seed_in_selected_install_does_not_create_cursor(self):
        self.journal.touch()
        self.accept()
        result = self.drain('g2', {self.key: self.initial})
        self.assertEqual((result['state'], result['reason']), ('blocked', 'snapshot_missing'))
        self.assertFalse(discovery._paths(self.decision)[0].exists())

    def test_pending_over_cap_is_bounded_and_grant_is_spent(self):
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.accept()
        more = {self.key: 'd' * 64, self.project.raw_name + '/docs/other.md': 'e' * 64}
        with patch.object(discovery, 'MAX_PENDING', 1):
            result = self.drain('g2', more)
        self.assertEqual((result['state'], result['reason'], result['skipped']),
                         ('drained', 'pending_over_cap_skipped', 2))
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [])
        self.assertEqual(discovery._read(discovery._paths(self.decision)[0])['pending'], {})
