"""S5 L0 R1: the provider identity proof asks a fixture port only when one is given or set up."""
import os
import unittest
from unittest import mock

from memvane.journal import api as journal
from memvane.journal.types import JournalEvidenceError


class _Port:
    def __init__(self):
        self.calls = []

    def provider_manifest(self, record, profile, *, allow_local_unavailable=False):
        self.calls.append((record, profile, allow_local_unavailable))
        return {'fixture': True}, True


class FixtureProof(unittest.TestCase):
    def environ(self):
        env = {k: v for k, v in os.environ.items() if k != 'MEMVANE_TEST_RUNTIME_ROOT'}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_an_explicit_port_proves(self):
        port = _Port()
        with self.environ():
            result = journal.provider_manifest({'r': 1}, {'p': 2}, allow_local_unavailable=True,
                                               provider=port)
        self.assertEqual((result, port.calls), (({'fixture': True}, True), [({'r': 1}, {'p': 2}, True)]))

    def test_a_port_refusal_is_journal_evidence(self):
        from memvane.provider.types import RuntimeUnavailable

        class Refusing:
            def provider_manifest(self, record, profile, *, allow_local_unavailable=False):
                raise RuntimeUnavailable('provider_identity_changed')
        with self.environ(), self.assertRaises(JournalEvidenceError) as caught:
            journal.provider_manifest({'r': 1}, {'p': 2}, provider=Refusing())
        self.assertEqual(str(caught.exception), 'provider_identity_changed')

    def test_without_a_port_or_test_root_the_record_is_proved(self):
        with self.environ(), self.assertRaises(JournalEvidenceError) as caught:
            journal.provider_manifest({'r': 1}, {'p': 2})
        self.assertEqual(str(caught.exception), 'worker_attestation_invalid')


if __name__ == '__main__':
    unittest.main()
