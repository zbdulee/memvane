"""Which step recover runs (R1) and when a written selection may be released (D5).

``recover_origin_selection`` finishes a stopped activation or restore: with a
ledger it resumes the step the ledger names; without one (finalize never ran)
it only finishes an abandon whose journal already says ``restored`` and
otherwise refuses ``initial_build_not_finalized`` (abandoning takes the
approved restore). After the first selected write, restore releases the
selection only when this host's provider, model or runtime no longer matches
the attestation (D5): re-onboarding is then the supported path.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

from memvane.install.application import onboarding_activation as activation, onboarding_restore
from memvane.install.types import InstallBlocked
from memvane.journal.types import JournalEvidenceError

PROJECT = '/synthetic/project'
ROUTE = b'{"route":1}'


class RecoverRules(unittest.TestCase):
    def setUp(self):
        self.state = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvr-')))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        os.chmod(self.state, 0o700)
        self.journal = self.state / 'journal.sqlite'

    def write_ledger(self, value):
        path = self.state / 'ledger.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
        finally:
            os.close(fd)

    def recover(self, journal_state):
        """``(step taken, result)``; the restore and activate steps are recorded, not run."""
        taken = []
        with mock.patch('memvane.journal.api.inspect_journal', return_value={'state': journal_state}), \
                mock.patch.object(activation, '_checked_project', side_effect=Path), \
                mock.patch.object(activation, '_restore_v1_locked',
                                  side_effect=lambda project, state: taken.append(('restore', str(project),
                                                                                   state)) or 'restored'), \
                mock.patch.object(activation, 'activate_origin_selection',
                                  side_effect=lambda *args: taken.append(('activate',) + args) or 'selected'):
            result = activation.recover_origin_selection(PROJECT, self.journal, ROUTE)
        return taken, result

    def test_without_a_ledger_only_a_begun_abandon_is_finished(self):
        for journal_state in ('prepared', 'selected'):
            with self.subTest(journal=journal_state):
                with self.assertRaises(InstallBlocked) as caught:
                    self.recover(journal_state)
                self.assertEqual(str(caught.exception), 'initial_build_not_finalized')
        self.assertEqual(self.recover('restored'), ([('restore', PROJECT, self.state)], 'restored'))

    def test_a_ledger_resumes_the_step_it_names(self):
        identity = {'projectRoot': PROJECT}
        for phase, step in (('prepared', 'activate'), ('selected', 'activate'),
                            ('restoring', 'restore'), ('restored', 'restore')):
            with self.subTest(phase=phase):
                self.write_ledger({'schema': 'memvane-install-ledger-v1', 'epoch': 2, 'phase': phase,
                                   'identity': identity})
                # The journal state never overrides a ledger.
                taken, _result = self.recover('prepared')
                self.assertEqual(len(taken), 1)
                self.assertEqual(taken[0][0], step)
                if step == 'activate':
                    self.assertEqual(taken[0][1:], (PROJECT, self.journal, ROUTE))


class ProviderChanged(unittest.TestCase):
    """``_provider_changed``: the D5 check restore asks after the first selected write."""

    def journal(self, *, digest='a' * 64, manifest=None, error=None):
        def provider_manifest(attestation, profile, *, allow_local_unavailable):
            self.assertEqual((attestation, profile, allow_local_unavailable), ('attestation', 'profile', True))
            if error is not None:
                raise JournalEvidenceError(error)
            return manifest
        return types.SimpleNamespace(journal_attestation=lambda path: ('attestation', digest),
                                     provider_manifest=provider_manifest)

    def changed(self, journal):
        return onboarding_restore._provider_changed(journal, Path('/synthetic/journal.sqlite'),
                                                    {'attestationSha256': 'a' * 64, 'vectorProfile': 'profile'})

    def test_only_a_provider_that_no_longer_matches_releases(self):
        cases = ((self.journal(manifest=({}, True)), False),
                 (self.journal(manifest=({}, False)), True),
                 (self.journal(error='provider_identity_changed'), True))
        for journal, expected in cases:
            with self.subTest(expected=expected):
                self.assertIs(self.changed(journal), expected)

    def test_other_evidence_failures_refuse(self):
        with self.assertRaises(JournalEvidenceError) as caught:
            self.changed(self.journal(error='provider_manifest_invalid'))
        self.assertEqual(str(caught.exception), 'provider_manifest_invalid')
        with self.assertRaises(InstallBlocked) as caught:
            self.changed(self.journal(digest='b' * 64, manifest=({}, True)))
        self.assertEqual(str(caught.exception), 'worker_attestation_changed')


if __name__ == '__main__':
    unittest.main()
