"""Real temporary install metadata; only OS device numbers change at reboot."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
spec = importlib.util.spec_from_file_location('reboot_seed', HERE / 'laya-journal-reader-checks.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
import laya_install_journal as J
import laya_host_response as response


class RebootStat:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    @property
    def st_dev(self):
        return self.original.st_dev + 1

    def __getitem__(self, key):
        values = list(self.original)
        values[2] += 1
        return values[key]

    def __iter__(self):
        return iter(self[:])


@contextmanager
def simulated_reboot():
    """Keep fd/path observations consistent and preserve nanosecond metadata."""
    def wrap(original):
        def stat(*args, **kwargs):
            result = original(*args, **kwargs)
            return result if isinstance(result, RebootStat) else RebootStat(result)
        return stat
    with ExitStack() as stack:
        for target, names in ((os, ('stat', 'lstat', 'fstat')),
                              (Path, ('stat', 'lstat'))):
            for name in names:
                stack.enter_context(mock.patch.object(target, name, wrap(getattr(target, name))))
        yield


class RebootChecks(unittest.TestCase):
    def setUp(self):
        self.helper = seed.JournalReaderChecks('runTest')
        self.addCleanup(self.helper.doCleanups)
        self.case = self.helper.published_case()

    def test_selected_hooks_notification_and_bundle_cas(self):
        case = self.case
        decision = case.decision()
        payload = {'cwd': str(case.project), 'tool_name': 'Edit',
                   'hook_event_name': 'PostToolUse',
                   'tool_input': {'file_path': str(case.project / 'notes.md')},
                   'tool_response': {'success': True}}
        with self.helper.full_authority(case), simulated_reboot(), \
             mock.patch.object(response, '__file__', str(case.plugin / 'core/laya_host_response.py')), \
             mock.patch('runtime_work.request', return_value={'status': 'queued'}), \
             mock.patch.object(response, '_start_owner'), \
             mock.patch.object(response, '_request_journal_v4'), \
             mock.patch.object(response, '_move_notices', return_value=None), \
             mock.patch('wiki_compile_enqueue.enqueue_payload', return_value={'queued': 0}):
            # A full selected proof must work before any read-only fallback.
            J.verify_selected_route(decision)
            for action in ('recall', 'gate', 'posttool', 'index', 'compile',
                           'update-queued', 'file-change-event'):
                event = dict(payload)
                if action == 'recall':
                    event = {'cwd': str(case.project), 'prompt': 'otter ledger'}
                elif action == 'gate':
                    event['hook_event_name'] = 'PreToolUse'
                elif action == 'update-queued':
                    event['hook_event_name'] = 'SessionStart'
                elif action == 'file-change-event':
                    event.update(tool_name='Bash', tool_input={'command': 'git switch main'},
                                 tool_response={'exit_code': 0})
                with self.subTest(action=action):
                    output = response.serve_selected(action, 'claude', decision, event)
                    if action == 'recall':
                        self.assertIn('otter', str(output))
            J.verify_notification_target(decision)
            newer = {**case.proof, 'sourceCommit': 'a' * 40,
                     'bundleManifestSha256': 'f' * 64}
            route = {**case.route, 'bundleManifestSha256': 'f' * 64}
            raw = J._canon(route).encode()
            row = J.inspect_journal(case.journal)
            with mock.patch.object(J, '_inspect_identity', return_value=newer), \
                 mock.patch.object(J, '_bind_attestation', return_value=(
                     'a' * 64, [[str(case.plugin / 'p.py'), 1, 2, 3, 4, 5]])):
                pending = J.cas_stage_update(case.journal, row['epoch'], newer, raw)
                case.private_write(case.auto / J.ROUTE_FILE, raw)
                active = J.cas_activate(case.journal, pending['epoch'], raw)
                self.assertEqual(active['state'], 'active')
                older = J.cas_stage_update(case.journal, active['epoch'], case.proof, case.route_raw)
                case.private_write(case.auto / J.ROUTE_FILE, case.route_raw)
                recovered = J.cas_recover(case.journal, older['epoch'], case.route_raw)
                self.assertEqual(recovered['state'], 'active')


if __name__ == '__main__':
    unittest.main()
