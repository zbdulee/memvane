"""Setup entry dispatch of ``state-cleanup``: the preview's digest, the approval bound to it and the exit code.

The install and provider sides are stand-ins that record how they were
called; what a cleanup removes is ``test_state_cleanup_110`` in the install tests.
"""
import contextlib
import io
import json
import unittest
from unittest import mock

GUARD = 'memvane.kernel.memvane_execution_guard.require_cached_plugin_for_live_write'
CLEANUP = 'memvane.install.api.state_cleanup'
RUNTIME = 'memvane.provider.api.collect'


class Fakes:
    """Installs and runtime generations that would be removed, and every call made."""

    def __init__(self, installs=('a' * 32,), generations=('g-old',), roots=('/state/memvane',)):
        self.installs, self.generations, self.roots = list(installs), list(generations), list(roots)
        self.calls = []
        self.removed = 'collected'
        self.runtime_removed = 'collected'

    def cleanup(self, home=None, *, approve=False, only=None, everything=False):
        self.calls.append(('cleanup', approve, None if only is None else sorted(only), everything))
        if everything:
            rows = [{'root': 'state', 'path': path, 'action': 'remove'} for path in self.roots]
            return {'status': self.removed if approve else 'planned', 'roots': rows,
                    'changesApplied': len(rows) if approve else 0}
        rows = [{'install': name, 'action': 'remove', 'reason': 'released'} for name in self.installs]
        rows.append({'install': 'b' * 32, 'action': 'keep', 'reason': 'selected'})
        return {'status': self.removed if approve else 'planned', 'installs': rows,
                'changesApplied': len(self.installs) if approve else 0}

    def collect(self, home=None, *, approve=False):
        self.calls.append(('runtime', approve))
        rows = [{'generation': name, 'action': 'remove'} for name in self.generations]
        rows.append({'generation': 'g-active', 'action': 'keep', 'reason': 'active'})
        return {'status': self.runtime_removed if approve else 'planned', 'generations': rows,
                'changesApplied': len(self.generations) if approve else 0}


class CleanupSetupEntry(unittest.TestCase):
    def run_cli(self, fakes, *args, guard=None):
        from memvane.app import setup_entry
        out = io.StringIO()
        with mock.patch(GUARD, side_effect=guard) as held, mock.patch(CLEANUP, side_effect=fakes.cleanup), \
                mock.patch(RUNTIME, side_effect=fakes.collect), contextlib.redirect_stdout(out):
            code = setup_entry.main(['state-cleanup', *args])
        return code, json.loads(out.getvalue()), held.call_count

    def test_the_preview_removes_nothing_and_names_what_an_approval_is_for(self):
        fakes = Fakes()
        code, row, guarded = self.run_cli(fakes)
        self.assertEqual((code, row['status'], guarded), (0, 'planned', 0))
        self.assertRegex(row['previewSha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(fakes.calls, [('cleanup', False, None, False), ('runtime', False)])
        # The same list gives the same digest; another list gives another.
        self.assertEqual(self.run_cli(Fakes())[1]['previewSha256'], row['previewSha256'])
        for other in (Fakes(installs=('a' * 32, 'c' * 32)), Fakes(generations=()), Fakes(installs=())):
            self.assertNotEqual(self.run_cli(other)[1]['previewSha256'], row['previewSha256'])

    def test_an_approval_of_the_previewed_list_removes_exactly_that_list(self):
        fakes = Fakes()
        digest = self.run_cli(fakes)[1]['previewSha256']
        fakes.calls.clear()
        code, row, guarded = self.run_cli(fakes, '--approve-state-cleanup', digest)
        self.assertEqual((code, row['status'], row['changesApplied'], guarded), (0, 'collected', 2, 1))
        self.assertEqual(fakes.calls, [('cleanup', False, None, False), ('runtime', False),
                                       ('cleanup', True, ['a' * 32], False), ('runtime', True)])

    def test_an_approval_of_another_list_removes_nothing_and_shows_the_current_one(self):
        fakes = Fakes()
        digest = self.run_cli(fakes)[1]['previewSha256']
        fakes.installs.append('c' * 32)  # Released after the owner saw the preview.
        fakes.calls.clear()
        code, row, guarded = self.run_cli(fakes, '--approve-state-cleanup', digest)
        self.assertEqual((code, row['status'], row['reason'], row['changesApplied'], guarded),
                         (1, 'rejected', 'state_cleanup_preview_changed', 0, 0))
        self.assertNotEqual(row['previewSha256'], digest)
        self.assertEqual([call for call in fakes.calls if call[1]], [])
        for wrong in ('', 'yes', '0' * 64):
            self.assertEqual(self.run_cli(fakes, '--approve-state-cleanup', wrong)[1]['status'], 'rejected')

    def test_everything_has_its_own_preview_and_approval(self):
        fakes = Fakes()
        code, preview, _ = self.run_cli(fakes, '--all-state')
        self.assertEqual((code, preview['status']), (0, 'planned'))
        self.assertEqual(fakes.calls, [('cleanup', False, None, True)])
        # The digest of a plain cleanup never approves the removal of everything, nor the reverse.
        plain = self.run_cli(Fakes())[1]['previewSha256']
        self.assertEqual(self.run_cli(fakes, '--all-state', '--approve-state-cleanup', plain)[1]['status'], 'rejected')
        self.assertEqual(self.run_cli(Fakes(), '--approve-state-cleanup', preview['previewSha256'])[1]['status'],
                         'rejected')
        fakes.calls.clear()
        code, row, guarded = self.run_cli(fakes, '--all-state', '--approve-state-cleanup', preview['previewSha256'])
        self.assertEqual((code, row['status'], guarded), (0, 'collected', 1))
        self.assertEqual(fakes.calls, [('cleanup', False, None, True), ('cleanup', True, ['/state/memvane'], True)])

    def test_whatever_is_left_behind_fails_the_command(self):
        for installs, runtime in (('partial', 'collected'), ('collected', 'partial'), ('collected', 'blocked')):
            fakes = Fakes()
            digest = self.run_cli(fakes)[1]['previewSha256']
            fakes.removed, fakes.runtime_removed = installs, runtime
            code, row, _ = self.run_cli(fakes, '--approve-state-cleanup', digest)
            self.assertEqual((code, row['status']), (1, 'partial'))

    def test_a_runtime_collection_that_fails_after_the_installs_were_removed_keeps_their_result(self):
        # W17 r2 review F-10: the runtime lock was taken between the preview and the removal.
        fakes = Fakes()
        digest = self.run_cli(fakes)[1]['previewSha256']
        collect = fakes.collect

        def busy(home=None, *, approve=False):
            if approve:
                raise ValueError('laya_embed_runtime_busy')
            return collect(home, approve=approve)
        fakes.collect = busy
        fakes.calls.clear()
        code, row, _ = self.run_cli(fakes, '--approve-state-cleanup', digest)
        self.assertEqual((code, row['status'], row['changesApplied']), (1, 'partial', 1))
        self.assertEqual(row['indexRuntime'], {'status': 'blocked', 'reason': 'laya_embed_runtime_busy',
                                               'changesApplied': 0})
        self.assertEqual([item['action'] for item in row['installs'] if item['install'] == 'a' * 32], ['remove'])
        self.assertIn(('cleanup', True, ['a' * 32], False), fakes.calls)

    def test_a_machine_without_an_embedding_runtime_still_cleans_installs(self):
        fakes = Fakes()
        fakes.collect = mock.Mock(side_effect=ValueError('laya_embed_runtime_missing'))
        code, preview, _ = self.run_cli(fakes)
        self.assertEqual(preview['indexRuntime'], {'status': 'unavailable', 'reason': 'laya_embed_runtime_missing',
                                                   'changesApplied': 0})
        code, row, _ = self.run_cli(fakes, '--approve-state-cleanup', preview['previewSha256'])
        self.assertEqual((code, row['status'], row['changesApplied']), (0, 'collected', 1))
        self.assertEqual(fakes.collect.call_count, 2)  # Two previews; nothing of the runtime to remove.

    def test_a_refused_live_write_guard_removes_nothing(self):
        fakes = Fakes()
        digest = self.run_cli(fakes)[1]['previewSha256']
        fakes.calls.clear()
        code, row, _ = self.run_cli(fakes, '--approve-state-cleanup', digest,
                                    guard=ValueError('memvane_live_write_requires_cached_plugin'))
        self.assertEqual((code, row['status'], row['changesApplied']), (1, 'rejected', 0))
        self.assertEqual([call for call in fakes.calls if call[1]], [])
