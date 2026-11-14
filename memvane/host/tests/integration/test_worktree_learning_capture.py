"""Linked worktree recall cannot record the main checkout's learning event."""
from types import SimpleNamespace
from unittest.mock import patch

from memvane.host import host_response as response
from memvane.journal import api as journal
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class WorktreeLearningCapture(FixtureCase):
    def test_linked_recall_clears_learning_capture(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'synthetic source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        build_install(self, project)
        linked = project.linked_worktree('linked', 'linked-branch')
        decision = journal.select_route(str(linked.root), action='recall')
        self.assertEqual(decision['state'], 'worktree')
        def answer(*args, **kwargs):
            response._set_learning_capture(('capture', 'must-not-run'))
            return SimpleNamespace(own_view=True)
        rendered = {'hookSpecificOutput': {'additionalContext': 'synthetic context'}}
        with patch.object(response, '_endpoint_answer', side_effect=answer), \
             patch.object(response, '_render_answer', return_value=rendered), \
             response.learning_capture() as capture:
            output = response.serve_selected('recall', 'claude', decision,
                {'cwd': str(linked.root), 'hook_event_name': 'UserPromptSubmit', 'prompt': 'synthetic'})
        self.assertEqual(capture, [])
        self.assertIn('synthetic context', output['hookSpecificOutput']['additionalContext'])
