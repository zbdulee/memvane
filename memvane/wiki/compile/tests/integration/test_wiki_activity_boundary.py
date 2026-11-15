"""Wiki activity is confined to the selected private installation state."""
from memvane.diagnostics import api as diagnostics
from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project


class WikiActivityBoundary(FixtureCase):
    def test_private_state_only_refuses_symlink_and_loose_paths(self):
        project = make_project(self.home, git=False)
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        bound = ({'journalPath': str(state / 'journal.sqlite'), 'installId': 'a' * 32}, {})
        self.assertFalse(workspace.append_activity(project.root, 'created', 'concepts/a.md', 'A', None))
        self.assertTrue(workspace.append_activity(project.root, 'created', 'concepts/a.md', 'A\nB',
                                                  bound, reason='compile'))
        text = diagnostics.wiki_tail(state)
        self.assertIn('concepts/a.md - A B', text)
        self.assertNotIn('sha256', text)
        (project.wiki / 'log.md').write_text('PRIVATE_ROOT_SENTINEL')
        orientation = worker.orientation(project.root, project.wiki, {}, bound)
        self.assertEqual(orientation['logTail'], text)
        self.assertNotIn('PRIVATE_ROOT_SENTINEL', repr(orientation))

        link = self.home / 'link'
        link.symlink_to(state, target_is_directory=True)
        loose = self.home / 'loose'
        loose.mkdir(mode=0o755)
        for bad in (link, loose):
            with self.subTest(state=bad.name):
                invalid = ({'journalPath': str(bad / 'journal.sqlite'), 'installId': 'a' * 32}, {})
                self.assertFalse(workspace.append_activity(project.root, 'created', 'concepts/b.md', 'B', invalid))
        self.assertFalse((loose / 'events.jsonl').exists())
        self.assertNotIn('concepts/b.md', diagnostics.wiki_tail(state))
