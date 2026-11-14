"""A real selected installation recognizes only proven linked worktrees."""
import json

from memvane.journal import api as journal
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class WorktreeRouteBoundary(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        note = self.project.write_note('docs/note.md', 'synthetic knowledge\n')
        self.project.write_card('concepts/note.md', title='Note', body='synthetic knowledge\n',
                                sources=(note,))
        self.project.git('add', '--', '.memvane/settings.json', 'docs/note.md',
                         '.memvane/wiki/concepts/note.md')
        self.project.git('commit', '-qm', 'selected fixture')
        self.install = build_install(self, self.project)

    def test_nested_and_sibling_worktrees_classify_read_only(self):
        main = journal.select_route(str(self.project.root), action='recall')
        self.assertEqual(main['state'], 'candidate')
        nested = self.project.root / '.claude/worktrees/agent'
        nested.parent.mkdir(parents=True)
        self.project.git('worktree', 'add', '-qb', 'nested-branch', str(nested))
        sibling = self.project.linked_worktree('sibling', 'sibling-branch').root
        for root in (nested, sibling):
            for cwd in (root, root / '.memvane'):
                with self.subTest(cwd=str(cwd)):
                    decision = journal.select_route(str(cwd), action='recall')
                    self.assertEqual(decision['state'], 'worktree', decision)
                    self.assertEqual(decision['worktreeRoot'], str(root))
                    self.assertEqual(decision['mainRoute']['projectRoot'], str(self.project.root))

    def test_unproven_identity_keeps_todays_classification(self):
        linked = self.project.linked_worktree('changed', 'changed-branch')
        settings = json.loads(linked.settings_path.read_text())
        settings['name'] = 'other-project'
        linked.settings_path.write_text(json.dumps(settings))
        self.assertNotEqual(journal.select_route(str(linked.root))['state'], 'worktree')
        from memvane.route import types as route_types
        own = self.project.linked_worktree('own-route', 'own-route-branch')
        (own.root / '.memvane' / route_types.RECORD).write_text('{}')
        self.assertEqual(journal.select_route(str(own.root))['state'], 'blocked')
        clone = self.home / 'clone'
        self.project.git('clone', '-q', str(self.project.root), str(clone))
        self.assertEqual(journal.select_route(str(clone))['state'], 'legacy')

    def test_unpublished_worktree_never_falls_back_to_main_recall(self):
        from unittest.mock import patch
        from memvane.host import api as host
        from memvane.runtime import api as runtime
        linked = self.project.linked_worktree('unpublished', 'unpublished-branch')
        decision = journal.select_route(str(linked.root), action='recall')
        self.assertEqual(decision['state'], 'worktree')
        with patch.object(runtime, 'request') as wake:
            answer = host.serve_selected('recall', 'claude', decision, {
                'session_id': 'fixture', 'cwd': str(linked.root),
                'hook_event_name': 'UserPromptSubmit', 'prompt': 'synthetic knowledge'})
        context = ((answer or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
        self.assertEqual(context, '')
        wake.assert_not_called()

    def test_published_worktree_recalls_its_own_view_without_waking(self):
        from unittest.mock import patch
        from memvane.host import api as host
        from memvane.runtime import api as runtime
        linked = self.project.linked_worktree('published', 'published-branch')
        linked.write_card('concepts/note.md', title='Distinct worktree',
                          body='The violet otter marker belongs to this worktree.\n',
                          sources=(linked.root / 'docs/note.md',))
        linked.git('add', '--', '.memvane/wiki/concepts/note.md')
        linked.git('commit', '-qm', 'worktree-specific card')
        for _ in range(8):
            self.install.run_sync()
            decision = journal.select_route(str(linked.root), action='recall')
            self.assertEqual(decision['state'], 'worktree')
            with patch.object(runtime, 'request') as wake:
                answer = host.serve_selected('recall', 'claude', decision, {
                    'session_id': 'fixture-published', 'cwd': str(linked.root),
                    'hook_event_name': 'UserPromptSubmit', 'prompt': 'violet otter marker'})
            wake.assert_not_called()
            context = ((answer or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
            if 'violet otter' in context.lower():
                break
        self.assertIn('violet otter', context.lower())

    def test_repeated_session_start_coalesces_lifecycle_request(self):
        from unittest.mock import patch
        from memvane.host import api as host
        from memvane.runtime import api as runtime
        from memvane.wiki.workspace import api as workspace
        decision = self.install.select()
        payload = {'hook_event_name': 'SessionStart', 'cwd': str(self.project.root),
                   'session_id': 'fixture-session'}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}) as wake:
            first = host.serve_selected('update-queued', 'claude', decision, payload)
            second = host.serve_selected('update-queued', 'claude', decision, payload)
        self.assertEqual(first, second)
        self.assertEqual([call.args[0] for call in wake.call_args_list].count('laya-compile'), 1)
        claim = workspace.claim_lifecycle(self.project.root)
        self.assertEqual(claim['trigger'], 'session_start')
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))

    def test_unselected_session_start_records_no_lifecycle_request(self):
        from memvane.wiki.workspace import api as workspace
        self.assertIsNone(workspace.request_lifecycle({'state': 'legacy'}, 'session_start'))
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))

    def test_edit_and_successful_bash_trigger_one_coalesced_lifecycle_request(self):
        from unittest.mock import patch
        from memvane.host import api as host
        from memvane.runtime import api as runtime
        from memvane.wiki.workspace import api as workspace
        decision = self.install.select()
        edit = {'hook_event_name': 'PostToolUse', 'tool_name': 'Write',
                'tool_input': {'file_path': str(self.project.root / 'docs/note.md')},
                'cwd': str(self.project.root)}
        bash = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                'tool_input': {'command': 'touch docs/note.md'},
                'tool_response': {'exit_code': 0}, 'cwd': str(self.project.root)}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}) as wake:
            self.assertEqual(host.serve_selected('index', 'claude', decision, edit), {})
            self.assertEqual(host.serve_selected('file-change-event', 'claude', decision, bash), {})
        self.assertEqual([call.args[0] for call in wake.call_args_list].count('laya-compile'), 1)
        self.assertEqual(workspace.claim_lifecycle(self.project.root)['trigger'], 'source_edit')
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))

    def test_lifecycle_request_failure_does_not_change_hook_answer(self):
        from unittest.mock import patch
        from memvane.host import api as host
        from memvane.runtime import api as runtime
        from memvane.wiki.workspace import api as workspace
        decision = self.install.select()
        payload = {'hook_event_name': 'SessionStart', 'cwd': str(self.project.root),
                   'session_id': 'fixture-session'}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            baseline = host.serve_selected('update-queued', 'claude', decision, payload)
            with patch.object(workspace, 'request_lifecycle', side_effect=OSError('unwritable')):
                self.assertEqual(host.serve_selected('update-queued', 'claude', decision, payload), baseline)

    def test_repeated_session_start_is_served_by_one_compile_pass(self):
        from unittest.mock import patch
        from memvane.host import api as host
        from memvane.runtime import api as runtime
        from memvane.wiki.compile import api as compile_api
        from memvane.wiki.workspace import api as workspace
        decision = self.install.select()
        payload = {'hook_event_name': 'SessionStart', 'cwd': str(self.project.root),
                   'session_id': 'fixture-session'}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}) as wake:
            host.serve_selected('update-queued', 'claude', decision, payload)
            host.serve_selected('update-queued', 'claude', decision, payload)
            self.assertEqual([call.args[0] for call in wake.call_args_list].count('laya-compile'), 1)
            result = compile_api.run(self.project.root)
        self.assertEqual(result, {'state': 'drained', 'processed': 0})
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))

    def test_failed_lifecycle_child_does_not_leave_the_request_pending(self):
        from unittest.mock import patch
        from memvane.wiki.compile import api as compile_api
        from memvane.wiki.lifecycle import api as lifecycle_api
        from memvane.wiki.workspace import api as workspace
        from memvane.runtime import api as runtime
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            self.assertEqual(workspace.request_lifecycle(self.install.select(), 'session_start'), 'recorded')
            with patch.object(lifecycle_api, 'run_lifecycle', return_value={'state': 'failed', 'reason': 'TimeoutExpired'}) as child:
                result = compile_api.run(self.project.root)
        child.assert_called_once()
        self.assertEqual(result, {'state': 'drained', 'processed': 0})
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))

    def test_linked_worktree_session_does_not_request_lifecycle(self):
        from unittest.mock import patch
        from memvane.host import api as host
        from memvane.runtime import api as runtime
        from memvane.wiki.workspace import api as workspace
        linked = self.project.linked_worktree('no-lifecycle', 'no-lifecycle-branch')
        decision = journal.select_route(str(linked.root), action='update-queued')
        self.assertEqual(decision['state'], 'worktree')
        payload = {'hook_event_name': 'SessionStart', 'cwd': str(linked.root), 'session_id': 'linked'}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            host.serve_selected('update-queued', 'claude', decision, payload)
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))
        self.assertIsNone(workspace.claim_lifecycle(linked.root))
