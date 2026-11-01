"""Out-of-project edit routing between two real-shaped selected installs.

A's selected ``index`` hook sends a private bounded hint (path, git blob or
``deleted``, originating host) to an already-selected owner B and offers B's
existing ``laya-sync`` wake. Discovery proves B from journal, route and bundle
metadata only (``laya_install_journal.verify_notification_target``): the hook
never opens B's index or claim DB, never starts a provider or model and never
activates anything. B's owner drain repeats the full route/store/write proof,
the current blob and current settings/compile consent before it enqueues.

Every project, journal and state write stays under a temporary HOME. Only the
bound plugin bundle read and the full selected-route proof (which needs an
installed executing bundle) are replaced.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(FIXTURES.parents[1] / 'core'))
sys.path.insert(1, str(FIXTURES))
import compile_paths  # noqa: E402
import laya_embed_resident  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_index_host_route as routes  # noqa: E402
import laya_install_journal as journal  # noqa: E402
import laya_product_sync_worker as sync  # noqa: E402
import onboarding_choice  # noqa: E402
import runtime_work  # noqa: E402
import out_of_project_installs as installs  # noqa: E402

# Imported before any patch. The full proofs are patched on
# laya_install_journal, so the module must look them up at call time (as
# laya_host_response and wiki_selected_laya do), never bind them at import.
try:
    import laya_external_edits
except ImportError:
    laya_external_edits = None

LOADED_ROOT = str(Path(response.__file__).resolve().parents[1])
WAKE_KINDS = {'laya-sync', 'laya-compile'}


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def external():
    if laya_external_edits is None:
        raise AssertionError('core/laya_external_edits.py is missing')
    return laya_external_edits


class Routing(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='xroute-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        env = patch.dict(os.environ, {
            'HOME': str(self.home), 'QMD_SANDBOX': '1', 'QMD_SETUP_GUARD_FIXTURE': '1',
            'MEMVANE_TEST_RUNTIME_ROOT': str(self.home / 'runtime-root'),
            'GIT_CONFIG_NOSYSTEM': '1'})
        env.start()
        self.addCleanup(env.stop)
        work = self.home / 'work'
        self.a = installs.make_install(self.home, work / 'alpha', 'alpha', '3' * 32, '6' * 32)
        self.b = installs.make_install(self.home, work / 'beta', 'beta', '5' * 32, '7' * 32)
        self.owners = [self.b]
        self.bundles = installs.BundleReader([self.a, self.b])
        self.patch(journal, 'inspect_full_plugin_candidate', side_effect=self.bundles)
        self.file = self.b.project / 'note.md'
        self.file.write_text('B knowledge\n')
        self.full_calls, self.full_failures = [], {}
        self.proofs = {
            str(install.project): {'status': 'installer_active_full_plugin_verified',
                                   'productRouteAllowed': True, 'selectionEpoch': 2,
                                   'executingPluginRoot': LOADED_ROOT,
                                   'projectRoot': str(install.project),
                                   'installId': install.install_id,
                                   'providerManifest': {'providerPath': 'p.py'}}
            for install in (self.a, self.b)}
        self.patch(journal, 'verify_selected_route', side_effect=self.full_proof)
        self.patch(journal, 'verify_selected_route_for_write',
                   side_effect=lambda decision, action='update': self.full_proof(decision))
        self.wake = self.patch(runtime_work, 'request', return_value={'status': 'queued'})
        self.patch(response, '_request_wiki_lifecycle')

    def patch(self, obj, name, **kw):
        patcher = patch.object(obj, name, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    # Full selected proof: needs the executing bundle, so it is replaced. The
    # hook guard records which projects it was asked to prove.
    def full_proof(self, decision, action='update'):
        root = decision.get('projectRoot') if isinstance(decision, dict) else None
        self.full_calls.append(root)
        if root in self.full_failures:
            raise journal.JournalEvidenceError(self.full_failures[root])
        if root not in self.proofs or decision.get('state') != 'candidate':
            raise journal.JournalEvidenceError('unselected_host_route')
        return dict(self.proofs[root])

    @contextlib.contextmanager
    def hook_guard(self):
        """A's hook: no owner store open, no provider/model, no activation, no full owner proof."""
        start = len(self.full_calls)
        opened, forbidden = [], []
        denied = tuple(str(path) for owner in self.owners for path in (owner.db, owner.queue))
        real_connect = sqlite3.connect

        def connect(target, *args, **kwargs):
            if any(path in str(target) for path in denied):
                opened.append(str(target))
                raise sqlite3.OperationalError('owner_store_opened_by_hook')
            return real_connect(target, *args, **kwargs)

        def forbid(name):
            def call(*args, **kwargs):
                forbidden.append(name)
                raise AssertionError(name)
            return call
        modes = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sqlite3, 'connect', connect))
            for name in ('cas_activate', 'cas_stage_update', 'cas_mark_new_write',
                         'cas_recover', 'cas_restore'):
                stack.enter_context(patch.object(journal, name, forbid(name)))
            for name in ('ensure_running', 'probe'):
                stack.enter_context(patch.object(laya_embed_resident, name, forbid(name)))
            for path in denied:
                if os.path.exists(path):
                    modes[path] = os.stat(path).st_mode & 0o777
                    os.chmod(path, 0)
            try:
                yield
            finally:
                for path, mode in modes.items():
                    os.chmod(path, mode)
        self.assertEqual(opened, [])
        self.assertEqual(forbidden, [])
        self.assertLessEqual(set(self.full_calls[start:]), {str(self.a.project)},
                             'the hook ran a full proof of another project')

    def decision(self, install, action='update'):
        return routes.select_route(str(install.project), action=action)

    def event(self, path=None, **extra):
        return {'cwd': str(self.a.project), 'hook_event_name': 'PostToolUse',
                'tool_name': 'Write', 'session_id': 'test-session',
                'tool_input': {'file_path': str(path or self.file), 'content': 'x'}, **extra}

    def index(self, event=None, host='codex'):
        with self.hook_guard():
            return response.serve_selected('index', host, self.decision(self.a, 'index'),
                                           event or self.event())

    def wakes(self):
        return [call.args[:2] for call in self.wake.call_args_list]

    def inbox(self, install):
        path = external()._inbox_path(self.decision(install))
        return json.loads(path.read_text()) if path.exists() else None

    def queue_rows(self, install):
        path = install.folder / 'compile' / compile_paths.SOURCE_QUEUE
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def change_owner_route(self):
        """B's visible route moves away from the one its journal selected.

        Without the selection marker the record alone still classifies as
        candidate, so only the journal's selected route digest refuses it.
        """
        (self.b.folder / installs.fresh.MARKER).unlink()
        route = {**self.b.route, 'generation': 'g-' + '5' * 32}
        installs.private_write(self.b.folder / routes.RECORD, installs.canon(route))

    def assert_not_routed(self, install=None):
        install = install or self.b
        self.assertNotIn(('laya-sync', str(install.project)), self.wakes())
        self.assertIsNone(self.inbox(install))
        self.assertFalse((install.folder / 'compile').exists())


class NotificationTargetProof(Routing):
    """``verify_notification_target`` on real journals: metadata only."""

    def target(self, decision):
        with self.hook_guard():
            return journal.verify_notification_target(decision)

    def test_selected_owner_is_proved_without_its_stores(self):
        decision = self.decision(self.b)
        self.assertEqual(decision['state'], 'candidate', decision)
        self.assertIn('recallSettings', decision)
        self.assertNotIn('settings', decision)
        proof = self.target(decision)
        self.assertIs(proof['notificationOnly'], True)
        self.assertNotIn('productRouteAllowed', proof)
        self.assertEqual(proof['projectRoot'], str(self.b.project))
        self.assertEqual(proof['installId'], self.b.install_id)
        self.assertEqual(proof['journalPath'], str(self.b.journal))

    def test_unselected_decision_states_are_refused(self):
        decision = self.decision(self.b)
        for state in ('legacy', 'blocked', 'preview', 'worktree', 'upgrading'):
            with self.subTest(state=state), self.assertRaises(journal.JournalEvidenceError):
                self.target({**decision, 'state': state})

    def test_stale_decision_is_refused(self):
        decision = self.decision(self.b)
        changed = {**decision, 'recallSettings': {**decision['recallSettings'],
                                                  'skipPaths': ['other']}}
        with self.assertRaises(journal.JournalEvidenceError):
            self.target(changed)

    def test_owner_route_change_is_refused(self):
        self.change_owner_route()
        decision = self.decision(self.b)
        self.assertEqual(decision['state'], 'candidate', decision)
        with self.assertRaises(journal.JournalEvidenceError):
            self.target(decision)

    def test_inactive_journal_is_refused(self):
        pending = installs.make_install(self.home, self.home / 'work' / 'gamma', 'gamma',
                                        '8' * 32, '9' * 32, activate=False)
        self.owners.append(pending)
        self.bundles.add(pending)
        decision = self.decision(pending)
        self.assertEqual(decision['state'], 'candidate', decision)
        with self.assertRaises(journal.JournalEvidenceError):
            self.target(decision)

    def test_bundle_drift_is_refused(self):
        self.bundles.drifted.add(str(self.b.plugin))
        with self.assertRaises(journal.JournalEvidenceError):
            self.target(self.decision(self.b))

    def test_changed_attestation_is_refused(self):
        sealed = json.loads(self.b.attestation.read_text())
        installs.private_write(self.b.attestation, journal._canon(
            {**sealed, 'dependencyClosure': ['changed']}).encode('utf-8'))
        with self.assertRaises(journal.JournalEvidenceError):
            self.target(self.decision(self.b))

    def test_unsafe_journal_parent_is_refused(self):
        decision = self.decision(self.b)
        self.b.state.chmod(0o755)
        self.addCleanup(self.b.state.chmod, 0o700)
        with self.assertRaises(journal.JournalEvidenceError):
            self.target(decision)


class HookRouting(Routing):
    def test_external_edit_hints_and_wakes_only_B(self):
        self.assertEqual(self.index(), {})
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())
        self.assertNotIn(('laya-sync', str(self.a.project)), self.wakes())
        self.assertLessEqual({kind for kind, _ in self.wakes()}, WAKE_KINDS)
        self.assertEqual([root for kind, root in self.wakes() if kind != 'laya-sync'], [])
        path = external()._inbox_path(self.decision(self.b))
        self.assertIn(self.b.state, path.parents)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('B knowledge', path.read_text())
        value = json.loads(path.read_text())
        self.assertEqual(value['edits'], [{'path': 'note.md',
                                           'blob': blob(self.file.read_bytes()),
                                           'host': 'codex'}])
        self.assertFalse(external()._inbox_path(self.decision(self.a)).exists())
        self.assertFalse((self.b.folder / 'compile').exists())

    def test_origin_host_is_preserved(self):
        for host in ('claude', 'codex', 'hermes'):
            with self.subTest(host=host):
                self.file.write_text('origin ' + host + '\n')
                self.index(host=host)
                self.assertEqual(self.inbox(self.b)['edits'][-1]['host'], host)

    def test_dedup_same_blob_and_newer_blob(self):
        self.index()
        self.index()
        self.assertEqual(len(self.inbox(self.b)['edits']), 1)
        self.file.write_text('B knowledge, revised\n')
        self.index()
        edits = self.inbox(self.b)['edits']
        keys = [(row['path'], row['blob']) for row in edits]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(('note.md', blob(self.file.read_bytes())), keys)

    def test_return_to_a_processed_revision_sends_a_new_hint(self):
        original = self.file.read_bytes()
        self.index()
        external().drain(self.decision(self.b), recheck=lambda: None)
        self.file.write_text('intermediate revision\n')
        self.index()
        external().drain(self.decision(self.b), recheck=lambda: None)
        self.file.write_bytes(original)
        self.index()
        self.assertIn(blob(original), [row['blob'] for row in self.inbox(self.b)['edits']])

    def test_revert_while_a_newer_hint_waits_keeps_the_current_revision(self):
        original = self.file.read_bytes()
        self.index()
        external().drain(self.decision(self.b), recheck=lambda: None)
        self.file.write_text('pending revision\n')
        self.index()
        self.file.write_bytes(original)
        self.index()
        self.assertEqual(self.inbox(self.b)['edits'][-1]['blob'], blob(original))

    def test_restored_source_reuses_content_without_suppressing_its_hint(self):
        original = self.file.read_bytes()
        self.index()
        external().drain(self.decision(self.b), recheck=lambda: None)
        self.file.unlink()
        self.index()
        external().drain(self.decision(self.b), recheck=lambda: None)
        self.file.write_bytes(original)
        self.index()
        self.assertIn(blob(original), [row['blob'] for row in self.inbox(self.b)['edits']])

    def test_inbox_is_capped_and_overflow_still_wakes(self):
        cap = external().MAX_INBOX_EDITS
        self.assertIsInstance(cap, int)
        self.assertGreater(cap, 0)
        for number in range(cap + 3):
            path = self.b.project / f'n{number}.md'
            path.write_text(f'note {number}\n')
            self.wake.reset_mock()
            self.index(self.event(path))
            self.assertIn(('laya-sync', str(self.b.project)), self.wakes())
        self.assertLessEqual(len(self.inbox(self.b)['edits']), cap)

    def test_deleted_source_hint(self):
        self.file.unlink()
        self.index()
        self.assertEqual(self.inbox(self.b)['edits'],
                         [{'path': 'note.md', 'blob': 'deleted', 'host': 'codex'}])
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())

    def test_onboarding_never_and_skip_do_not_disable_a_selected_owner(self):
        onboarding_choice.never(str(self.b.project))
        self.index()
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())
        onboarding_choice.resume(str(self.b.project))
        onboarding_choice.skip(str(self.b.project), 'test-session')
        self.wake.reset_mock()
        self.index()
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())

    def test_unonboarded_owner_does_not_route(self):
        (self.b.folder / routes.RECORD).unlink()
        (self.b.folder / installs.fresh.MARKER).unlink()
        self.assertEqual(self.decision(self.b)['state'], 'legacy')
        before = sorted(p.name for p in self.b.folder.iterdir())
        self.assertEqual(self.index(), {})
        self.assertNotIn(('laya-sync', str(self.b.project)), self.wakes())
        self.assertEqual(sorted(p.name for p in self.b.folder.iterdir()), before)

    def test_disabled_and_blocked_owner_do_not_route(self):
        for case in ('indexing_off', 'route_garbage'):
            with self.subTest(case=case):
                saved = {path: path.read_bytes()
                         for path in (self.b.settings, self.b.folder / routes.RECORD)}
                if case == 'indexing_off':
                    self.b.settings.write_text(json.dumps(
                        installs.legacy_settings(self.b, indexing=False)))
                else:
                    installs.private_write(self.b.folder / routes.RECORD, b'{"schema":')
                self.assertEqual(self.decision(self.b)['state'], 'blocked')
                self.wake.reset_mock()
                self.assertEqual(self.index(), {})
                self.assertNotIn(('laya-sync', str(self.b.project)), self.wakes())
                for path, raw in saved.items():
                    path.write_bytes(raw)

    def test_drifted_owner_does_not_route(self):
        self.bundles.drifted.add(str(self.b.plugin))
        self.assertEqual(self.index(), {})
        self.assertNotIn(('laya-sync', str(self.b.project)), self.wakes())

    def test_changed_owner_route_does_not_route(self):
        self.change_owner_route()
        self.assertEqual(self.decision(self.b)['state'], 'candidate')
        self.assertEqual(self.index(), {})
        self.assertNotIn(('laya-sync', str(self.b.project)), self.wakes())

    def test_inactive_preview_build_does_not_route(self):
        pending = installs.make_install(self.home, self.home / 'work' / 'gamma', 'gamma',
                                        '8' * 32, '9' * 32, activate=False)
        self.owners.append(pending)
        self.bundles.add(pending)
        path = pending.project / 'draft.md'
        path.write_text('pending\n')
        self.assertEqual(self.index(self.event(path)), {})
        self.assertNotIn(('laya-sync', str(pending.project)), self.wakes())
        self.assertFalse((pending.folder / 'compile').exists())

    def test_incomplete_owner_discovery_does_not_choose_an_earlier_parent(self):
        nested = installs.make_install(self.home, self.b.project / 'nested', 'nested',
                                        '8' * 32, '9' * 32)
        self.owners.append(nested)
        self.bundles.add(nested)
        path = nested.project / 'note.md'
        path.write_text('nested owner\n')
        with patch.object(external(), 'MAX_INSTALLS', 2):
            self.assertEqual(self.index(self.event(path)), {})
        self.assertNotIn(('laya-sync', str(self.b.project)), self.wakes())
        self.assertNotIn(('laya-sync', str(nested.project)), self.wakes())

    @unittest.skipUnless(shutil.which('git'), 'git unavailable')
    def test_linked_worktree_of_owner_does_not_route(self):
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
               '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main']
        run = lambda *args: subprocess.run([*git, *args], cwd=self.b.project, check=True,
                                           capture_output=True, text=True, timeout=30)
        run('init', '-q')
        run('add', 'note.md', '.memvane/settings.json')
        run('commit', '-qm', 'init')
        for worktree in (self.home / 'work' / 'beta-wt',
                         self.b.project / 'worktrees' / 'nested-wt'):
            with self.subTest(worktree=str(worktree)):
                run('worktree', 'add', '-q', str(worktree))
                self.assertEqual(routes.select_route(str(worktree), action='index')['state'], 'worktree')
                (worktree / 'note.md').write_text('worktree edit\n')
                self.assertEqual(self.index(self.event(worktree / 'note.md')), {})
                self.assert_not_routed()

    def test_failed_edit_does_not_route(self):
        for failure in ({'is_error': True}, {'success': False}, {'interrupted': True}):
            with self.subTest(failure=failure):
                self.assertEqual(self.index(self.event(tool_response=failure)), {})
                self.assert_not_routed()

    def test_hidden_denied_wiki_and_skip_paths_do_not_route(self):
        paths = [self.b.project / '.hidden.md', self.b.project / 'node_modules' / 'x.md',
                 self.b.project / 'docs' / '.draft' / 'y.md',
                 self.b.folder / 'wiki' / 'card.md', self.b.folder / 'settings.json']
        for path in paths:
            with self.subTest(path=str(path.relative_to(self.b.project))):
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text('private\n')
                self.index(self.event(path))
                self.assertNotIn(('laya-sync', str(self.b.project)), self.wakes())
                self.assertIsNone(self.inbox(self.b))
        # skipPaths come from B's current settings file; the selection stays.
        self.b.settings.write_text(json.dumps({'name': 'beta', 'skipPaths': ['excluded']}))
        self.assertEqual(self.decision(self.b)['recallSettings']['skipPaths'], ['excluded'])
        excluded = self.b.project / 'excluded' / 'skip.md'
        excluded.parent.mkdir()
        excluded.write_text('excluded\n')
        self.index(self.event(excluded))
        self.assertNotIn(('laya-sync', str(self.b.project)), self.wakes())
        self.index()
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())
        self.assertEqual([row['path'] for row in self.inbox(self.b)['edits']], ['note.md'])

    def test_symlink_escapes_do_not_route(self):
        private = self.a.project / 'private.md'
        private.write_text('A only\n')
        loose = self.home / 'loose'
        loose.mkdir()
        (loose / 'z.md').write_text('outside every project\n')
        (self.b.project / 'link.md').symlink_to(private)
        (self.b.project / 'out.md').symlink_to(loose / 'z.md')
        (self.b.project / 'linked').symlink_to(loose, target_is_directory=True)
        for path in (self.b.project / 'out.md', self.b.project / 'linked' / 'z.md'):
            with self.subTest(path=path.name):
                self.assertEqual(self.index(self.event(path)), {})
                self.assert_not_routed()
        # Resolves inside A: A's own wake only.
        self.index(self.event(self.b.project / 'link.md'))
        self.assertIn(('laya-sync', str(self.a.project)), self.wakes())
        self.assert_not_routed()

    def test_mixed_patch_routes_each_path_to_its_owner(self):
        (self.a.project / 'local.md').write_text('A\n')
        loose = self.home / 'loose.md'
        loose.write_text('nobody\n')
        hidden = self.b.project / '.hidden.md'
        hidden.write_text('hidden\n')
        event = self.event(tool_name='apply_patch', tool_input={'patch': (
            f'*** Update File: local.md\n*** Update File: {self.file}\n'
            f'*** Update File: {loose}\n*** Update File: {hidden}\n')})
        self.assertEqual(self.index(event), {})
        self.assertIn(('laya-sync', str(self.a.project)), self.wakes())
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())
        self.assertEqual([row['path'] for row in self.inbox(self.b)['edits']], ['note.md'])
        # The public compile action accepts the mixed event and queues A only.
        response.serve_selected('compile', 'codex', self.decision(self.a, 'compile'), event)
        self.assertFalse((self.b.folder / 'compile').exists())
        self.assertEqual([row['source']['path'] for row in self.queue_rows(self.a)], ['local.md'])

    def test_multiedit_path_routes(self):
        self.index(self.event(tool_name='MultiEdit', tool_input={
            'file_path': str(self.file),
            'edits': [{'old_string': 'B', 'new_string': 'B2'}]}))
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())

    def test_multiedit_nested_paths_without_top_level_path_route(self):
        self.index(self.event(tool_name='MultiEdit', tool_input={'edits': [
            {'file_path': str(self.file), 'old_string': 'B', 'new_string': 'B2'}]}))
        self.assertIn(('laya-sync', str(self.b.project)), self.wakes())

    def test_notebook_path_is_validated_without_becoming_a_text_source(self):
        path = self.b.project / 'notebook.ipynb'
        path.write_text('{}')
        self.index(self.event(tool_name='NotebookEdit', tool_input={'notebook_path': str(path)}))
        self.assert_not_routed()

    def test_unsafe_owner_inbox_never_fails_A_or_escapes(self):
        path = external()._inbox_path(self.decision(self.b))
        path.parent.mkdir(parents=True, exist_ok=True)
        outside = self.home / 'untouched.json'
        outside.write_text('{}')
        path.symlink_to(outside)
        (self.a.project / 'local.md').write_text('A\n')
        event = self.event(tool_name='apply_patch', tool_input={'patch': (
            f'*** Update File: local.md\n*** Update File: {self.file}\n')})
        self.assertEqual(self.index(event), {})
        self.assertEqual(outside.read_text(), '{}')
        self.assertIn(('laya-sync', str(self.a.project)), self.wakes())

    def test_public_gate_and_observer_allow_external_paths(self):
        for host in ('claude', 'codex', 'hermes'):
            for action in ('gate', 'posttool'):
                with self.subTest(host=host, action=action):
                    event = self.event(hook_event_name='PreToolUse' if action == 'gate'
                                       else 'PostToolUse')
                    with self.hook_guard():
                        result = response.serve_selected(
                            action, host, self.decision(self.a, action), event)
                    self.assertIsInstance(result, dict)
        self.assert_not_routed()


class OwnerTieBreak(unittest.TestCase):
    def owner(self, path, *roots):
        targets = [{'projectRoot': root, 'rawRoot': root} for root in roots]
        chosen = external().owner_for(Path(path), targets)
        return None if chosen is None else chosen['projectRoot']

    def test_longest_raw_root_prefix_wins(self):
        self.assertEqual(self.owner('/w/b/sub/x.md', '/w/b', '/w/b/sub'), '/w/b/sub')
        self.assertEqual(self.owner('/w/b/x.md', '/w/b', '/w/b/sub'), '/w/b')

    def test_prefix_is_by_path_component(self):
        self.assertIsNone(self.owner('/w/bb/x.md', '/w/b'))
        self.assertIsNone(self.owner('/elsewhere/x.md', '/w/b'))

    def test_ties_reject_without_fallback(self):
        self.assertIsNone(self.owner('/w/b/x.md', '/w/b', '/w/b'))
        self.assertIsNone(self.owner('/w/b/sub/x.md', '/w', '/w/b/sub', '/w/b/sub'))


class OwnerDrain(Routing):
    def drain(self, recheck=lambda: None, decision=None):
        return external().drain(decision or self.decision(self.b), recheck=recheck)

    def test_drain_repeats_full_proof_and_compiles_current_blob(self):
        self.index()
        self.full_calls.clear()
        self.drain()
        self.assertIn(str(self.b.project), self.full_calls)
        rows = self.queue_rows(self.b)
        self.assertEqual([row['source']['path'] for row in rows], ['note.md'])
        self.assertEqual(rows[0]['engine'], 'codex')
        self.assertFalse((self.a.folder / 'compile').exists())
        self.drain()
        self.assertEqual(len(self.queue_rows(self.b)), 1)

    def test_stale_blob_is_not_compiled(self):
        self.index()
        self.file.write_text('new unrelated bytes\n')
        self.drain()
        self.assertEqual(self.queue_rows(self.b), [])

    def test_stale_deleted_hint_is_not_compiled(self):
        self.file.unlink()
        self.index()
        self.file.write_text('restored\n')
        self.drain()
        self.assertEqual(self.queue_rows(self.b), [])

    def test_current_deletion_requests_existing_owner_lifecycle(self):
        import wiki_lifecycle_request
        self.file.unlink()
        self.index()
        with patch.object(wiki_lifecycle_request, 'request') as request:
            self.drain()
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0]['projectRoot'], str(self.b.project))
        self.assertEqual(request.call_args.args[1], 'source_edit')
        self.assertEqual(self.queue_rows(self.b), [])

    def test_only_the_current_of_several_blobs_compiles(self):
        self.index()
        self.file.write_text('second\n')
        self.index()
        self.drain()
        rows = self.queue_rows(self.b)
        self.assertEqual([row['source']['path'] for row in rows], ['note.md'])

    def test_stale_uncompiled_hint_can_be_notified_again_when_current(self):
        original = self.file.read_bytes()
        self.index()
        self.file.write_text('unobserved replacement\n')
        self.drain()
        self.assertEqual(self.queue_rows(self.b), [])
        self.file.write_bytes(original)
        self.index()
        self.assertIn(blob(original), [row['blob'] for row in self.inbox(self.b)['edits']])

    def test_disabled_compile_does_not_suppress_a_later_consented_event(self):
        self.index()
        self.b.settings.write_text(json.dumps(installs.legacy_settings(self.b)))
        self.drain()
        self.assertEqual(self.queue_rows(self.b), [])
        self.b.settings.write_text(json.dumps({'name': 'beta', 'skipPaths': []}))
        self.index()
        self.assertEqual([row['path'] for row in self.inbox(self.b)['edits']], ['note.md'])

    def test_new_hint_during_owner_hash_is_not_lost_or_blocked(self):
        edits = external()
        original_revision = edits._revision
        self.index()
        armed = [True]

        def interleave(path):
            revision = original_revision(path)
            if path == self.file and armed[0]:
                armed[0] = False
                self.file.write_text('arrives while the owner works\n')
                self.index()
            return revision
        with patch.object(edits, '_revision', side_effect=interleave):
            self.drain()
        self.assertEqual([row['blob'] for row in self.inbox(self.b)['edits']],
                         [blob(self.file.read_bytes())])
        self.assertEqual(self.queue_rows(self.b), [])

    def test_failed_owner_proof_keeps_the_hint_for_retry(self):
        self.index()
        self.full_failures[str(self.b.project)] = 'project_dir_split'
        with self.assertRaises(Exception):
            self.drain()
        self.assertEqual(self.queue_rows(self.b), [])
        self.assertEqual(len(self.inbox(self.b)['edits']), 1)
        del self.full_failures[str(self.b.project)]
        self.drain()
        self.assertEqual([row['source']['path'] for row in self.queue_rows(self.b)], ['note.md'])

    def test_changed_proof_during_drain_keeps_the_hint(self):
        self.index()

        def changed():
            raise ValueError('selected_proof_changed')
        with self.assertRaises(Exception):
            self.drain(recheck=changed)
        self.assertEqual(self.queue_rows(self.b), [])
        self.assertEqual(len(self.inbox(self.b)['edits']), 1)

    def test_current_skip_paths_apply_at_drain(self):
        drafts = self.b.project / 'drafts' / 'x.md'
        drafts.parent.mkdir()
        drafts.write_text('draft\n')
        self.index(self.event(drafts))
        self.b.settings.write_text(json.dumps({'name': 'beta', 'skipPaths': ['drafts']}))
        self.drain()
        self.assertEqual(self.queue_rows(self.b), [])

    def test_compile_consent_off_never_compiles(self):
        self.index()
        self.b.settings.write_text(json.dumps(installs.legacy_settings(self.b)))
        decision = self.decision(self.b)
        self.assertEqual(decision['state'], 'candidate', decision)
        self.drain(decision=decision)
        self.assertEqual(self.queue_rows(self.b), [])

    def test_inbox_symlink_is_refused(self):
        self.index()
        path = external()._inbox_path(self.decision(self.b))
        path.unlink()
        outside = self.home / 'untouched.json'
        outside.write_text('{"edits": [{"path": "note.md", "blob": "%s", "host": "codex"}]}'
                           % blob(self.file.read_bytes()))
        before = outside.read_text()
        path.symlink_to(outside)
        with self.assertRaises(Exception):
            self.drain()
        self.assertEqual(outside.read_text(), before)
        self.assertEqual(self.queue_rows(self.b), [])


class SyncOwner(Routing):
    def test_completed_sync_drains_hints_without_discovery_grant(self):
        edits = external()
        drain = self.patch(edits, 'drain')
        decision = self.decision(self.b)
        proof = self.proofs[str(self.b.project)]
        result = sync._after_sync(decision, proof, None, {'state': 'unchanged'})
        self.assertEqual(result['state'], 'unchanged')
        drain.assert_called_once()
        self.assertEqual(drain.call_args.args[0], decision)
        recheck = drain.call_args.kwargs['recheck']
        with patch.object(sync, 'verify_selected_route', return_value=proof):
            recheck()
        with patch.object(sync, 'verify_selected_route',
                          return_value={**proof, 'selectionEpoch': 9}), \
                self.assertRaises(Exception):
            recheck()

    def test_unfinished_sync_keeps_hints(self):
        drain = self.patch(external(), 'drain')
        for state in ('deferred', 'pending', 'blocked'):
            with self.subTest(state=state):
                sync._after_sync(self.decision(self.b), self.proofs[str(self.b.project)],
                                 None, {'state': state})
        drain.assert_not_called()


if __name__ == '__main__':
    unittest.main()
