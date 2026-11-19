"""An edit recall import failure cannot change a hook answer or its exit code.

The posttool tail answers ``{}`` with exit 0, and a prompt recall still
serves its cards when ``remember_cards`` cannot be imported.
"""
from __future__ import annotations

import builtins
import json
import unittest
from unittest import mock

from memvane.host.tests.support import PROMPT, HostInstallCase
from memvane.host.tests.integration.test_edit_recall import NEW_FILE, EDIT_TEXT, edit_payload

_REAL_IMPORT = builtins.__import__


def failing_import(module, name):
    """``__import__`` that fails only ``from <module> import <name>``."""
    calls = []

    def fake(target, globals=None, locals=None, fromlist=(), level=0):
        if target == module and fromlist and name in fromlist:
            calls.append(target)
            raise ImportError('synthetic import failure')
        return _REAL_IMPORT(target, globals, locals, fromlist, level)
    return fake, calls


class EditRecallImportFailure(HostInstallCase):
    def setUp(self):
        super().setUp()
        self.fixture.write_note(NEW_FILE, EDIT_TEXT)

    def test_a_tail_import_failure_answers_empty_with_exit_zero(self):
        fake, calls = failing_import('memvane.host', 'edit_recall')
        with mock.patch.object(builtins, '__import__', side_effect=fake):
            code, out, err = self.main('posttool', edit_payload('import-tail'))
        self.assertEqual(calls, ['memvane.host'])
        self.assertEqual((code, json.loads(out)), (0, {}), err)

    def test_prompt_recall_serves_when_remember_cards_cannot_be_imported(self):
        fake, calls = failing_import('memvane.host.edit_recall', 'remember_cards')
        with mock.patch.object(builtins, '__import__', side_effect=fake):
            code, out, err = self.main('recall', {'prompt': PROMPT, 'session_id': 'import-prompt',
                                                  'hook_event_name': 'UserPromptSubmit'})
        self.assertEqual(calls, ['memvane.host.edit_recall'])
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', out)


if __name__ == '__main__':
    unittest.main()
