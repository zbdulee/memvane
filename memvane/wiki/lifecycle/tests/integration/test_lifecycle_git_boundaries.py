"""Lifecycle holds generated cards across real interrupted Git operations."""
from unittest.mock import patch

from memvane.wiki.lifecycle import wiki_lifecycle as lifecycle
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class LifecycleGitBoundaries(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.root = self.project.root
        self.project.write_note('conflict.txt', 'base\n')
        self.project.git('add', '--', '.memvane/settings.json', 'conflict.txt')
        self.project.git('commit', '-qm', 'initial')
        use_config(self, self.project, expanded(self.project))

    def test_rebase_in_progress_skips_whole_lifecycle_run(self):
        self.project.git('checkout', '-qb', 'topic')
        self.project.write_note('conflict.txt', 'topic\n')
        self.project.git('add', '--', 'conflict.txt')
        self.project.git('commit', '-qm', 'topic')
        self.project.git('checkout', '-q', 'main')
        self.project.write_note('conflict.txt', 'main\n')
        source = self.project.write_note('docs/gone.md', 'source before deletion\n')
        card = self.project.write_card('concepts/card.md', title='Card', body='content\n',
                                       sources=(source,))
        self.project.git('add', '--', 'conflict.txt', 'docs/gone.md',
                         '.memvane/wiki/concepts/card.md')
        self.project.git('commit', '-qm', 'main')
        self.project.git('rm', '-q', '--', 'docs/gone.md')
        self.project.git('commit', '-qm', 'delete source')
        self.project.git('checkout', '-q', 'topic')
        self.project.git('rebase', 'main', check=False)
        try:
            self.assertTrue((self.root / '.git/rebase-merge').exists()
                            or (self.root / '.git/rebase-apply').exists())
            with patch.object(workspace, 'selected', side_effect=AssertionError('route reached')):
                result = lifecycle.run(str(self.root))
            self.assertTrue(card.exists())
            self.assertEqual(result['examined'], 0)
            self.assertIn(result.get('gitOperation'), ('rebase-merge', 'rebase-apply'))
        finally:
            self.project.git('rebase', '--abort', check=False)

    def test_lifecycle_skips_linked_worktree(self):
        source = self.project.write_note('docs/source.md', 'source\n')
        self.project.write_card('concepts/card.md', title='Card', body='content\n', sources=(source,))
        self.project.git('add', '--', 'docs/source.md', '.memvane/wiki/concepts/card.md')
        self.project.git('commit', '-qm', 'source and card')
        linked = self.project.linked_worktree('linked', 'linked-branch')
        linked_card = linked.wiki / 'concepts/card.md'
        use_config(self, linked, expanded(linked))
        with patch.object(workspace, 'selected', side_effect=AssertionError('route reached')):
            result = lifecycle.run(str(linked.root))
        self.assertTrue(linked_card.exists())
        self.assertEqual(result['route'], 'worktree')
        self.assertEqual(result['examined'], 0)
