"""Selection proofs of a v1 journal: provider identity, writer floor and highest plugin version.

Reads need the selected route, journal, ledger, attestation and provider
identity; writes also need a running plugin at or above the journal's writer
floor (D4). The highest plugin version only rises, under the sync owner.
"""
import unittest

from memvane.journal.tests.support import InstallCase, file_state


def journal_api():
    from memvane.journal import api
    return api


def running_version():
    from memvane.kernel.memvane_version import plugin_version, version_key
    return plugin_version(), version_key(plugin_version())


class WriterFloor(InstallCase):
    def test_writer_floor_above_the_running_plugin_refuses_writes_only(self):
        journal = journal_api()
        store = file_state(self.install.store_path)
        self.forge("UPDATE selection SET writer_floor='99.0.0' WHERE id=1")
        proof = journal.verify_selected_route(self.decision)
        self.assertEqual(proof['writerFloor'], '99.0.0')
        self.assertTrue(self.endpoint_id())
        self.refused('writer_floor', journal.verify_selected_route_for_write, self.decision)
        with self.owner() as owner:
            self.refused('writer_floor', journal.selected_v1_writer_scope(self.decision, queue_owner=owner).__enter__)
        self.assertEqual(file_state(self.install.store_path), store)
        version, _key = running_version()
        self.forge('UPDATE selection SET writer_floor=? WHERE id=1', (version,))
        self.assertEqual(journal.verify_selected_route_for_write(self.decision)['writerFloor'], version)

    def test_unparsable_writer_floor_is_an_invalid_journal(self):
        journal = journal_api()
        for value in ('next', '1.0', ''):
            with self.subTest(floor=value):
                self.forge('UPDATE selection SET writer_floor=? WHERE id=1', (value,))
                self.refused('journal_value_invalid', journal.inspect_journal, self.install.journal)
                self.refused('journal_value_invalid', journal.verify_selected_route, self.decision)


class HighestPluginVersion(InstallCase):
    def test_highest_version_rises_only_under_the_live_owner(self):
        journal = journal_api()
        version, running = running_version()
        self.forge("UPDATE selection SET max_plugin_version='0.9.0' WHERE id=1")
        before = self.row()
        with self.owner() as expired:
            pass
        for owner in (object(), expired):
            with self.subTest(owner=type(owner).__name__):
                with self.assertRaises(Exception):
                    journal.publish_plugin_version(self.decision, owner, running)
                self.assertEqual(self.row(), before)
        with self.owner() as owner:
            journal.publish_plugin_version(self.decision, owner, running)
        row = self.row()
        self.assertEqual((row['maxPluginVersion'], row['epoch']), (version, before['epoch'] + 1))
        self.assertEqual(row['reader'], before['reader'])

    def test_an_older_or_equal_running_version_changes_nothing(self):
        journal = journal_api()
        _version, running = running_version()
        before = self.row()
        with self.owner() as owner:
            journal.publish_plugin_version(self.decision, owner, running)
            journal.publish_plugin_version(self.decision, owner, (0, 1, 0, (1,)))
        self.assertEqual(self.row(), before)

    def test_an_unselected_journal_refuses_the_version(self):
        journal = journal_api()
        _version, running = running_version()
        self.forge("UPDATE selection SET state='restored', max_plugin_version='0.9.0' WHERE id=1")
        before = self.row()
        with self.owner() as owner:
            self.refused('selector_version_changed', journal.publish_plugin_version,
                         self.decision, owner, running)
        self.assertEqual(self.row(), before)


class ProviderProof(InstallCase):
    def test_changed_provider_identity_refuses_reads_and_writes_until_it_returns(self):
        journal = journal_api()
        self.fixture_provider.change_identity()
        self.refused('provider_identity_changed', journal.verify_selected_route, self.decision)
        self.refused('provider_identity_changed', journal.verify_selected_route_for_write, self.decision)
        self.refused('provider_identity_changed', self.endpoint_id)
        with self.owner() as owner:
            self.refused('provider_identity_changed',
                         journal.selected_v1_writer_scope(self.decision, queue_owner=owner).__enter__)
        self.fixture_provider.change_identity(False)
        proof = journal.verify_selected_route_for_write(self.decision)
        self.assertEqual((proof['providerAvailable'], proof['productRouteAllowed']), (True, True))
        self.assertTrue(self.endpoint_id())

    def test_changed_attestation_refuses_every_proof(self):
        import json
        from memvane.journal.tests.support import canon
        journal = journal_api()
        attestation = self.state / 'provider-attestation.json'
        raw = attestation.read_bytes()
        changed = canon(dict(json.loads(raw), forged=True)).encode('utf-8')
        cases = (('production_attestation_noncanonical', raw.rstrip() + b' \n', 0o600),
                 ('worker_attestation_changed', changed, 0o600),
                 ('unsafe_production_attestation', raw, 0o644))
        for reason, data, mode in cases:
            with self.subTest(reason=reason):
                attestation.write_bytes(data)
                attestation.chmod(mode)
                for call in (journal.verify_selected_route, journal.verify_selected_route_for_write):
                    self.refused(reason, call, self.decision)
                self.refused(reason, self.endpoint_id)
        attestation.write_bytes(raw)
        attestation.chmod(0o600)
        self.assertTrue(journal.verify_selected_route(self.decision)['productRouteAllowed'])
        self.assertTrue(self.endpoint_id())

if __name__ == '__main__':
    unittest.main()
