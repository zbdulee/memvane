"""Discovery grants are durable, bounded and usable only after activation."""
from memvane.wiki.intake import wiki_discovery as discovery
from memvane.wiki.intake.types import DiscoveryBlocked
from tests.support.case import FixtureCase
from tests.support.project import make_project


class DiscoveryGrants(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        self.decision = {'scopeId': 'a' * 32, 'storeIncarnation': 'b' * 32,
                         'installId': 'c' * 32, 'storePath': str(state / 'store.sqlite'),
                         'projectRoot': str(self.project.root),
                         'collections': [
                             {'name': self.project.raw_name, 'role': 'raw', 'sourceRoot': str(self.project.root)},
                             {'name': self.project.wiki_name, 'role': 'wiki', 'sourceRoot': str(self.project.wiki)}],
                         'collectionRoots': {self.project.raw_name: str(self.project.root),
                                             self.project.wiki_name: str(self.project.wiki)}}

    def test_grants_record_activate_and_revoke_without_crossing_phases(self):
        grant = discovery.record_grant(self.decision, 'sync', engine='codex')
        observed = discovery.observe_grants(self.decision)
        self.assertEqual(observed['grants']['sync'], [])
        self.assertEqual(observed['provisional']['sync'], [grant])
        self.assertEqual(observed['engines'][grant], 'codex')
        self.assertTrue(discovery.activate_grant(self.decision, 'sync', grant))
        observed = discovery.observe_grants(self.decision)
        self.assertEqual(observed['grants']['sync'], [grant])
        self.assertEqual(observed['provisional']['sync'], [])
        self.assertTrue(discovery.revoke_grant(self.decision, grant))
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [])
        self.assertFalse(discovery.activate_grant(self.decision, 'sync', grant))

    def test_grant_recorded_after_snapshot_remains_distinct(self):
        first = discovery.record_grant(self.decision, 'sync')
        self.assertTrue(discovery.activate_grant(self.decision, 'sync', first))
        snapshot = discovery.observe_grants(self.decision)
        later = discovery.record_grant(self.decision, 'sync')
        self.assertTrue(discovery.activate_grant(self.decision, 'sync', later))
        self.assertEqual(snapshot['grants']['sync'], [first])
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [first, later])
        self.assertTrue(discovery.revoke_grant(self.decision, first))
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['sync'], [later])

    def test_corrupt_grants_fail_closed_for_worker_then_hook_quarantines(self):
        path = discovery._paths(self.decision)[1]
        path.write_text('{bad json')
        path.chmod(0o600)
        self.assertEqual(discovery.observe_grants(self.decision)['reason'], 'state_corrupt')
        grant = discovery.record_grant(self.decision, 'sync')
        quarantined = list(path.parent.glob(path.name + '.corrupt-*'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(), '{bad json')
        self.assertEqual(discovery.observe_grants(self.decision)['provisional']['sync'], [grant])

    def test_unsafe_grant_file_is_never_repaired_or_consumed(self):
        path = discovery._paths(self.decision)[1]
        path.write_text('{}')
        path.chmod(0o644)
        with self.assertRaisesRegex(DiscoveryBlocked, 'state_unsafe'):
            discovery.record_grant(self.decision, 'sync')
        self.assertEqual(path.read_text(), '{}')
        self.assertEqual(discovery.observe_grants(self.decision)['reason'], 'state_unsafe')

    def test_invalid_kind_or_engine_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'invalid_wiki_grant'):
            discovery.record_grant(self.decision, 'unknown')
        with self.assertRaisesRegex(ValueError, 'invalid_wiki_grant_engine'):
            discovery.record_grant(self.decision, 'sync', engine='unknown-host')
        self.assertFalse(discovery._paths(self.decision)[1].exists())

    def test_failed_grant_write_never_produces_usable_grant(self):
        from unittest.mock import patch
        with patch.object(discovery, '_write_grants', side_effect=OSError('write failed')):
            with self.assertRaisesRegex(OSError, 'write failed'):
                discovery.record_grant(self.decision, 'sync', engine='codex')
        observed = discovery.observe_grants(self.decision)
        self.assertEqual(observed['grants']['sync'], [])
        self.assertEqual(observed['provisional']['sync'], [])

    def test_uncertain_grant_writes_are_not_silently_accepted(self):
        from unittest.mock import patch
        real_write = discovery._write_grants
        def faults(*plan):
            steps = iter(plan)
            def write(*args, **kwargs):
                if next(steps) == 'landed':
                    real_write(*args, **kwargs)
                raise OSError('synthetic fsync failure')
            return write
        with patch.object(discovery, '_write_grants', side_effect=faults('landed', 'refused')):
            with self.assertRaises(OSError):
                discovery.record_grant(self.decision, 'sync')
        observed = discovery.observe_grants(self.decision)
        self.assertEqual(observed['grants']['sync'], [])
        self.assertEqual(len(observed['provisional']['sync']), 1)

        grant = discovery.record_grant(self.decision, 'session_start')
        with patch.object(discovery, '_write_grants', side_effect=faults('landed', 'landed')):
            with self.assertRaises(OSError):
                discovery.activate_grant(self.decision, 'session_start', grant)
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['session_start'], [])
        self.assertFalse(discovery.activate_grant(self.decision, 'session_start', grant))

        grant = discovery.record_grant(self.decision, 'session_start')
        with patch.object(discovery, '_write_grants', side_effect=faults('landed', 'refused')):
            with self.assertRaises(discovery.GrantUncertain):
                discovery.activate_grant(self.decision, 'session_start', grant)
        self.assertEqual(discovery.observe_grants(self.decision)['grants']['session_start'], [grant])
