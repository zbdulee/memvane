"""External edit routing through two selected synthetic 1.0 installations."""
import hashlib
from unittest.mock import patch

from memvane.registry import api as registry
from memvane.runtime import api as runtime
from memvane.wiki.intake import external_edits as external
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class ExternalInstallRouting(FixtureCase):
    def setUp(self):
        super().setUp()
        self.installs = []
        for index, ident in enumerate(('4' * 32, '5' * 32)):
            project = make_project(self.home, name='fixture%d' % index, dirname='project%d' % index)
            project.write_note('docs/note.md', 'initial %d\n' % index)
            project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
            project.git('commit', '-qm', 'fixture')
            install = build_install(self, project, install_id=ident)
            self.installs.append(install)
            registration = registry.after_setup('activate-index-origin', str(project.root),
                                                str(install.state), {'phase': 'selected', 'status': 'selected'})
            self.assertIsInstance(registration, dict)
            self.assertNotIn(registration.get('status'), ('unproved', 'skipped', 'unavailable'))
        for _ in range(8):
            if registry.reconcile(max_rows=64, seconds=60.0, backfill_page=64)['complete']:
                break
        else:
            self.fail('fixture registry did not complete')
        self.a, self.b = self.installs

    def test_external_edit_hints_and_wakes_only_selected_owner(self):
        source = self.b.project.write_note('docs/note.md', 'owner revision\n')
        import time
        rows = registry.routing_rows()
        targets = external._targets(str(self.a.project.root), time.monotonic() + 60)
        self.assertTrue(targets, 'registry=%r b_route=%r' % (rows, self.b.select()))
        with patch.object(runtime, 'request', return_value={'status': 'queued'}) as wake:
            result = external.notify([source], cwd=self.a.project.root, host='codex',
                                     origin=str(self.a.project.root))
        self.assertEqual(result, {'owners': 1, 'failed': 0, 'overflow': 0})
        wake.assert_called_once_with('laya-sync', str(self.b.project.root))
        decision = self.b.select()
        inbox = external._inbox_path(decision)
        row = external._read_box(inbox, decision)['edits'][0]
        body = source.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        self.assertEqual(row, {'path': 'docs/note.md', 'blob': blob, 'host': 'codex'})
        self.assertFalse(external._inbox_path(self.a.select()).exists())
        self.assertNotIn('owner revision', inbox.read_text())

    def test_notification_proof_reads_journal_without_opening_owner_store(self):
        import sqlite3
        from memvane.journal import api as journal
        from memvane.journal.types import JournalEvidenceError
        decision = self.b.select()
        original = sqlite3.connect
        opened = []
        def connect(target, *args, **kwargs):
            if str(self.b.store_path) in str(target):
                opened.append(str(target))
                raise AssertionError('notification opened owner store')
            return original(target, *args, **kwargs)
        with patch.object(sqlite3, 'connect', side_effect=connect):
            proof = journal.verify_notification_target(decision)
            self.assertTrue(proof['notificationOnly'])
            self.assertEqual(proof['projectRoot'], str(self.b.project.root))
            self.assertEqual(proof['installId'], self.b.install_id)
            self.assertEqual(opened, [])
            for state in ('legacy', 'blocked', 'preview', 'worktree', 'upgrading'):
                with self.subTest(state=state), self.assertRaises(JournalEvidenceError):
                    journal.verify_notification_target({**decision, 'state': state})
            changed = {**decision, 'recallSettings': {**decision['recallSettings'], 'skipPaths': ['other']}}
            with self.assertRaises(JournalEvidenceError):
                journal.verify_notification_target(changed)
        self.assertEqual(opened, [])

    def test_hook_rejects_failed_edits_then_routes_mixed_and_multiedit_paths(self):
        from memvane.host import api as host
        source = self.b.project.write_note('docs/note.md', 'external edit\n')
        local = self.a.project.write_note('docs/local.md', 'local edit\n')
        hidden = self.b.project.write_note('.hidden.md', 'hidden\n')
        loose = self.home / 'outside.md'
        loose.write_text('outside\n')
        decision = self.a.select()
        inbox = external._inbox_path(self.b.select())
        base = {'hook_event_name': 'PostToolUse', 'cwd': str(self.a.project.root),
                'tool_name': 'Write', 'tool_input': {'file_path': str(source)}}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}) as wake:
            for failure in ({'is_error': True}, {'success': False}, {'interrupted': True}):
                self.assertEqual(host.serve_selected('index', 'codex', decision,
                                                    {**base, 'tool_response': failure}), {})
            self.assertFalse(inbox.exists())
            wake.assert_not_called()
            mixed = {**base, 'tool_name': 'apply_patch', 'tool_input': {'patch': (
                '*** Update File: %s\n*** Update File: %s\n*** Update File: %s\n*** Update File: %s\n'
                % (local, source, hidden, loose))}}
            self.assertEqual(host.serve_selected('index', 'codex', decision, mixed), {})
            self.assertEqual({call.args for call in wake.call_args_list},
                             {('laya-sync', str(self.a.project.root)),
                              ('laya-sync', str(self.b.project.root)),
                              ('laya-compile', str(self.a.project.root))})
            self.assertEqual([row['path'] for row in external._read_box(inbox, self.b.select())['edits']],
                             ['docs/note.md'])
            for tool_input in ({'file_path': str(source), 'edits': [{'old_string': 'a', 'new_string': 'b'}]},
                               {'edits': [{'file_path': str(source), 'old_string': 'a', 'new_string': 'b'}]}):
                wake.reset_mock()
                event = {**base, 'tool_name': 'MultiEdit', 'tool_input': tool_input}
                self.assertEqual(host.serve_selected('index', 'codex', decision, event), {})
                wake.assert_called_once_with('laya-sync', str(self.b.project.root))
            notebook = self.b.project.write_note('book.ipynb', '{}')
            wake.reset_mock()
            self.assertEqual(host.serve_selected('index', 'codex', decision,
                {**base, 'tool_name': 'NotebookEdit', 'tool_input': {'notebook_path': str(notebook)}}), {})
            wake.assert_not_called()

    def test_notification_proof_rejects_unsafe_journal_parent(self):
        from memvane.journal import api as journal
        from memvane.journal.types import JournalEvidenceError
        decision = self.b.select()
        self.b.state.chmod(0o755)
        try:
            with self.assertRaises(JournalEvidenceError):
                journal.verify_notification_target(decision)
        finally:
            self.b.state.chmod(0o700)
