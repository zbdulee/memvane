"""A selected writer opens its store only after the full journal proof (fail-closed, design 8.4).

``selected_v1_writer_scope`` proves the sync owner, the reader lifetime and
the selected route, journal, ledger, attestation and provider before it reads
the store header. Any refusal issues no writer and leaves the store untouched.
"""
import unittest
from unittest import mock

from memvane.journal import writer_scope
from memvane.journal.tests.support import InstallCase, file_state
from memvane.journal.types import SelectedWriter


def journal_api():
    from memvane.journal import api
    return api


class WriterJournalProof(InstallCase):
    def watch(self):
        """Record journal proofs and store reads, in order, while the real ones run."""
        from memvane.journal import install_journal
        from memvane.store import api as store_api
        calls = []

        def recorded(label, real):
            def call(*args, **kwargs):
                calls.append(label)
                return real(*args, **kwargs)
            return call
        for target, owner, name in (
                ('memvane.journal.install_journal.verify_selected_route_for_write', install_journal,
                 'verify_selected_route_for_write'),
                ('memvane.store.api.read_header', store_api, 'read_header'),
                ('memvane.store.api.observe_binding', store_api, 'observe_binding')):
            patcher = mock.patch(target, recorded(name, getattr(owner, name)))
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def admit(self, decision=None):
        with self.writer(decision) as writer:
            return writer.admission_proof()

    def test_admission_reads_the_store_only_after_the_journal_proof(self):
        store = file_state(self.install.store_path)
        calls = self.watch()
        proof = self.admit()
        self.assertEqual(proof['selectionEpoch'], self.row()['epoch'])
        first_store = min(calls.index(name) for name in ('read_header', 'observe_binding'))
        self.assertEqual(calls[0], 'verify_selected_route_for_write')
        self.assertLess(calls.index('verify_selected_route_for_write'), first_store)
        self.assertEqual(file_state(self.install.store_path), store)

    def test_each_failed_proof_refuses_before_any_store_read(self):
        journal = journal_api()
        store = file_state(self.install.store_path)
        attestation = self.state / 'provider-attestation.json'
        route = self.project / '.memvane' / 'route.json'
        ledger = self.state / 'ledger.json'

        def floor(on):
            self.forge('UPDATE selection SET writer_floor=? WHERE id=1', ('99.0.0' if on else '1.0.0',))

        def provider(on):
            self.fixture_provider.change_identity(on)

        def attest(on, raw=attestation.read_bytes()):
            attestation.write_bytes(raw.rstrip() + b' \n' if on else raw)

        def route_file(on, raw=route.read_bytes()):
            route.write_bytes(raw.replace(b'}', b' }', 1) if on else raw)

        def transition(on, raw=ledger.read_bytes()):
            import json
            value = json.loads(raw)
            if on:
                journal.ledger_write(ledger, dict(value, phase='restoring'), replace=True)
            else:
                ledger.write_bytes(raw)
        cases = (('writer_floor', floor), ('provider_identity_changed', provider),
                 ('production_attestation_noncanonical', attest),
                 ('selection_transition_pending', transition))
        self.assertTrue(route.exists())
        cases += (('host_decision_changed', route_file),)
        for reason, change in cases:
            with self.subTest(reason=reason):
                calls = self.watch()
                change(True)
                try:
                    self.refused(reason, self.admit)
                finally:
                    change(False)
                self.assertNotIn('read_header', calls)
                self.assertNotIn('observe_binding', calls)
                self.assertEqual(writer_scope._selected_issued, {})
        self.assertEqual(file_state(self.install.store_path), store)
        self.assertEqual(self.admit()['selectionEpoch'], self.row()['epoch'])

    def test_an_expired_or_foreign_owner_is_refused_before_any_proof(self):
        journal = journal_api()
        calls = self.watch()
        with self.owner() as expired:
            pass
        other = self.tmp / 'other-store.sqlite'
        other.write_bytes(b'')
        other.chmod(0o600)
        with self.owner(other) as foreign:
            for owner in (expired, foreign, object()):
                with self.subTest(owner=owner.__class__.__name__):
                    with self.assertRaises(Exception):
                        journal.selected_v1_writer_scope(self.decision, queue_owner=owner).__enter__()
        self.assertEqual(calls, [])
        self.assertEqual(writer_scope._selected_issued, {})

    def test_owner_lost_during_admission_issues_nothing_and_releases_the_lifetime(self):
        journal = journal_api()
        real, checks = writer_scope.validate_sync_owner, []

        def second_check_fails(claim, queue_path):
            checks.append(queue_path)
            if len(checks) == 2:
                raise RuntimeError('owner_lost')
            return real(claim, queue_path)
        with mock.patch('memvane.journal.writer_scope.validate_sync_owner', second_check_fails):
            self.refused('owner_lost', self.admit)
        self.assertEqual(len(checks), 2)
        self.assertEqual(writer_scope._selected_issued, {})
        with journal.reader_lifetime(self.install.install_id, exclusive=True):
            pass
        self.assertEqual(self.admit()['selectionEpoch'], self.row()['epoch'])

    def test_caller_created_or_exited_writers_are_refused(self):
        self.refused('normal_writer_not_issued', SelectedWriter)
        forged = object.__new__(SelectedWriter)
        for call in (forged.validate, forged.store, forged.admission_proof, forged.mark_new_write):
            with self.subTest(call=call.__name__):
                self.refused('normal_writer_not_issued', call)
        with self.writer() as writer:
            writer.validate()
        for call in (writer.validate, writer.store, writer.admission_proof, writer.mark_new_write):
            with self.subTest(exited=call.__name__):
                self.refused('normal_writer_not_issued', call)


if __name__ == '__main__':
    unittest.main()
