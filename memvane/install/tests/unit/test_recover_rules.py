"""Which step recover runs (R1) and when a written selection may be released (D5).

``recover_origin_selection`` finishes a stopped activation or restore: with a
ledger it resumes the step the ledger names; without one (finalize never ran)
it only finishes an abandon whose journal already says ``restored`` and
otherwise refuses ``initial_build_not_finalized`` (abandoning takes the
approved restore). Restore releases the selection after a selected write as well.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from memvane.install.application import onboarding_activation as activation
from memvane.install.types import InstallBlocked

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


if __name__ == '__main__':
    unittest.main()
