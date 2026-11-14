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

    def test_fault_after_enqueue_retry_does_not_duplicate(self):
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.source.write_text('changed after seed\n')
        changed = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.accept()
        cursor = discovery._paths(self.decision)[0]
        real_write = discovery._write
        writes = [0]
        def fail_ack(path, value):
            if path == cursor:
                writes[0] += 1
                if writes[0] == 2:
                    raise OSError('simulated cursor ACK failure')
            return real_write(path, value)
        with patch.object(discovery, '_write', side_effect=fail_ack):
            with self.assertRaisesRegex(OSError, 'ACK failure'):
                self.drain('g2', {self.key: changed})
        queue = self.root / workspace.rel(workspace_types.SOURCE_QUEUE)
        before = queue.read_bytes()
        self.assertEqual(len(before.splitlines()), 1)
        result = self.drain('g2', {self.key: changed})
        self.assertEqual(result['state'], 'drained')
        self.assertEqual(queue.read_bytes(), before)
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [])

    def test_grant_recorded_after_snapshot_survives_consumption(self):
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.source.write_text('changed after seed\n')
        changed = hashlib.sha256(self.source.read_bytes()).hexdigest()
        first = self.accept()
        observed = discovery.observe_grants(self.decision)
        later = self.accept()
        with patch.object(workspace, 'selected', return_value=None):
            result = discovery.drain_after_sync(self.decision, observed, recheck=lambda: None,
                                               view_generation='g2', raw_snapshot={self.key: changed})
        self.assertEqual((result['state'], result['queued']), ('drained', 1))
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [later])
        self.assertNotIn(first, discovery.observe_grants(self.decision)['grants']['sync'])

    def test_seed_baseline_is_idempotent_and_changed_baseline_requires_inert_proof(self):
        from memvane.wiki.intake.types import DiscoveryBlocked
        first = {self.key: self.initial}
        self.assertEqual(discovery.seed_source_snapshot(self.decision, first, generation='g1'), first)
        self.assertEqual(discovery.seed_source_snapshot(self.decision, first, generation='g1'), first)
        changed = {self.key: 'f' * 64}
        with self.assertRaisesRegex(DiscoveryBlocked, 'baseline_changed'):
            discovery.seed_source_snapshot(self.decision, changed, generation='g2')
        self.assertEqual(discovery._read(discovery._paths(self.decision)[0])['rawSnapshot'], first)
        self.assertEqual(discovery.seed_source_snapshot(self.decision, changed, generation='g2',
                                                        replace_inert=True), changed)
        self.assertEqual(discovery._read(discovery._paths(self.decision)[0])['rawSnapshot'], changed)

    def test_changed_cursor_binding_and_roots_are_held_without_reset(self):
        import json
        path = discovery._paths(self.decision)[0]
        valid = discovery._state_doc(self.decision, 'g1', {self.key: self.initial}, {})
        for field, value, reason in (('scopeId', 'f' * 32, 'state_binding_mismatch'),
                                     ('rawCollections', ['other'], 'state_roots_changed')):
            with self.subTest(field=field):
                row = {**valid, field: value}
                encoded = json.dumps(row).encode()
                path.write_bytes(encoded)
                path.chmod(0o600)
                result = self.drain('g2', {self.key: self.initial})
                self.assertEqual((result['state'], result['reason']), ('blocked', reason))
                self.assertEqual(path.read_bytes(), encoded)

    def test_symlink_cursor_is_rejected_and_target_is_unchanged(self):
        path = discovery._paths(self.decision)[0]
        outside = self.home / 'outside.json'
        outside.write_text('{}')
        path.symlink_to(outside)
        result = self.drain('g2', {self.key: self.initial})
        self.assertEqual((result['state'], result['reason']), ('blocked', 'state_unreadable'))
        self.assertTrue(path.is_symlink())
        self.assertEqual(outside.read_text(), '{}')

    def test_blocked_run_retires_observed_grant_but_preserves_provisional_and_later(self):
        from memvane.wiki.intake.types import DiscoveryBlocked
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.source.write_text('changed\n')
        changed = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.drain('g2', {self.key: changed})
        first = self.accept()
        provisional = discovery.record_grant(self.decision, 'sync')
        observed = discovery.observe_grants(self.decision)
        later = discovery.record_grant(self.decision, 'session_start')
        self.assertTrue(discovery.activate_grant(self.decision, 'session_start', later))
        with patch.object(discovery, '_project_config', side_effect=DiscoveryBlocked('settings_changed')):
            result = discovery.drain_after_sync(self.decision, observed, recheck=lambda: None,
                                                view_generation='g2', raw_snapshot={self.key: changed})
        self.assertEqual((result['state'], result['reason']), ('blocked', 'settings_changed'))
        grants = discovery.observe_grants(self.decision)
        self.assertNotIn(first, grants['grants']['sync'])
        self.assertEqual(grants['provisional']['sync'], [provisional])
        self.assertEqual(grants['grants']['session_start'], [later])
        self.assertEqual(discovery._read(discovery._paths(self.decision)[0])['pending'], {self.key: changed})

    def test_revoke_after_snapshot_is_not_used_by_that_run(self):
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.source.write_text('changed\n')
        changed = hashlib.sha256(self.source.read_bytes()).hexdigest()
        grant = self.accept()
        observed = discovery.observe_grants(self.decision)
        self.assertTrue(discovery.revoke_grant(self.decision, grant))
        result = discovery.drain_after_sync(self.decision, observed, recheck=lambda: None,
                                           view_generation='g2', raw_snapshot={self.key: changed})
        self.assertEqual((result['state'], result['reason'], result['pending']),
                         ('pending', 'no_accepted_grant', 1))
        self.assertFalse((self.root / workspace.rel(workspace_types.SOURCE_QUEUE)).exists())

    def test_observed_provisional_grant_can_activate_before_drain(self):
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.source.write_text('changed\n')
        changed = hashlib.sha256(self.source.read_bytes()).hexdigest()
        grant = discovery.record_grant(self.decision, 'sync', engine='codex')
        observed = discovery.observe_grants(self.decision)
        self.assertTrue(discovery.activate_grant(self.decision, 'sync', grant))
        with patch.object(workspace, 'selected', return_value=None):
            result = discovery.drain_after_sync(self.decision, observed, recheck=lambda: None,
                                               view_generation='g2', raw_snapshot={self.key: changed})
        self.assertEqual((result['state'], result['queued']), ('drained', 1))

    def test_initial_seed_refuses_a_source_scan_over_the_file_cap(self):
        from memvane.wiki.intake.types import DiscoveryBlocked
        self.project.write_note('docs/second.md', 'second\n')
        with patch.object(discovery, 'RAW_SCAN_MAX_FILES', 1):
            with self.assertRaisesRegex(DiscoveryBlocked, 'raw_source_stale'):
                discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.assertFalse(discovery._paths(self.decision)[0].exists())

    def test_seed_rejects_foreign_paths_and_invalid_revision_without_state(self):
        from memvane.wiki.intake.types import DiscoveryBlocked
        bad = ({'other/docs/x.md': 'a' * 64}, {self.key: 'not-a-hash'},
               {self.project.raw_name + '/../outside.md': 'a' * 64})
        for baseline in bad:
            with self.subTest(baseline=baseline):
                with self.assertRaisesRegex(DiscoveryBlocked, 'baseline_invalid'):
                    discovery.seed_source_snapshot(self.decision, baseline, generation='g1')
                self.assertFalse(discovery._paths(self.decision)[0].exists())

    def test_failed_grant_removal_after_ack_never_pays_deferred_tail(self):
        import os
        discovery.seed_source_snapshot(self.decision, {self.key: self.initial}, generation='g1')
        self.source.write_text('changed\n')
        second = self.project.write_note('docs/second.md', 'other source\n')
        snapshot = {self.key: hashlib.sha256(self.source.read_bytes()).hexdigest(),
                    self.project.raw_name + '/docs/second.md': hashlib.sha256(second.read_bytes()).hexdigest()}
        grant = self.accept()
        observed = discovery.observe_grants(self.decision)
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '1'}), \
             patch.object(workspace, 'selected', return_value=None):
            with patch.object(discovery, '_write_grants', side_effect=OSError('synthetic disk full')):
                with self.assertRaises(OSError):
                    discovery.drain_after_sync(self.decision, observed, recheck=lambda: None,
                                               view_generation='g2', raw_snapshot=snapshot)
            queue = self.root / workspace.rel(workspace_types.SOURCE_QUEUE)
            self.assertEqual(len(queue.read_text().splitlines()), 1)
            state = discovery._read(discovery._paths(self.decision)[0])
            self.assertEqual(list(state['pending']), [self.project.raw_name + '/docs/second.md'])
            self.assertEqual(state['spentGrants'], [grant])
            retry = discovery.observe_grants(self.decision)
            self.assertEqual(retry['grants']['sync'], [grant])
            result = discovery.drain_after_sync(self.decision, retry, recheck=lambda: None,
                                               view_generation='g2', raw_snapshot=snapshot)
        self.assertEqual((result['state'], result['reason']), ('pending', 'no_accepted_grant'))
        self.assertEqual(len(queue.read_text().splitlines()), 1)
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [])
