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
