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

    def bound(self):
        return ({'projectRoot': str(self.root), 'installId': 'a' * 32,
                 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                 'recallSettings': {'skipPaths': []},
                 'collectionRoots': {self.project.raw_name: str(self.root),
                                     self.project.wiki_name: str(self.project.wiki)},
                 'collections': [{'name': self.project.raw_name, 'role': 'raw', 'sourceRoot': str(self.root)},
                                 {'name': self.project.wiki_name, 'role': 'wiki', 'sourceRoot': str(self.project.wiki)}]}, {})

    def run_selected(self):
        with patch.object(workspace, 'selected', return_value=self.bound()), \
             patch.object(workspace, 'recheck'), \
             patch.object(workspace, 'request_sync', return_value=True), \
             patch.object(workspace, 'request_compile'):
            return lifecycle.run(str(self.root))

    def create_card(self, name, source):
        return self.project.write_card('concepts/' + name, title='Card', body='<!-- qmd:auto:start id="main" sourceHash="a" -->\nfact\n<!-- qmd:auto:end -->\n',
                                       sources=(source,), status='generated',
                                       created_by='qmd-auto-context')

    def test_branch_switch_keeps_uncommitted_card_whose_source_is_only_on_main(self):
        self.project.git('branch', 'feature')
        source = self.project.write_note('docs/only-main.md', 'main-only source\n')
        self.project.git('add', '--', 'docs/only-main.md')
        self.project.git('commit', '-qm', 'main source')
        card = self.create_card('only-main.md', source)
        self.project.git('checkout', '-q', 'feature')
        self.assertFalse(source.exists())
        result = self.run_selected()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['held']), (0, 1))

    def test_deletion_committed_on_current_branch_retires_committed_card(self):
        source = self.project.write_note('docs/gone.md', 'source\n')
        card = self.create_card('gone.md', source)
        self.project.git('add', '--', 'docs/gone.md', '.memvane/wiki/concepts/gone.md')
        self.project.git('commit', '-qm', 'source and card')
        self.project.git('rm', '-q', '--', 'docs/gone.md')
        self.project.git('commit', '-qm', 'delete source')
        result = self.run_selected()
        self.assertFalse(card.exists())
        self.assertEqual((result['retired'], result['held']), (1, 0))

    def test_deletion_committed_on_current_branch_retires_uncommitted_card(self):
        source = self.project.write_note('docs/gone.md', 'source\n')
        self.project.git('add', '--', 'docs/gone.md')
        self.project.git('commit', '-qm', 'source')
        card = self.create_card('gone.md', source)
        self.project.git('rm', '-q', '--', 'docs/gone.md')
        self.project.git('commit', '-qm', 'delete source')
        result = self.run_selected()
        self.assertFalse(card.exists())
        self.assertEqual(result['retired'], 1)

    def test_uncommitted_working_tree_deletion_is_held(self):
        source = self.project.write_note('docs/tracked.md', 'source\n')
        card = self.create_card('tracked.md', source)
        self.project.git('add', '--', 'docs/tracked.md', '.memvane/wiki/concepts/tracked.md')
        self.project.git('commit', '-qm', 'source and card')
        source.unlink()
        result = self.run_selected()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['held']), (0, 1))

    def test_source_delete_job_on_other_branch_is_held_not_retired(self):
        import hashlib
        from memvane.wiki.lifecycle import wiki_source_retire as retire
        self.project.git('branch', 'feature')
        source = self.project.write_note('docs/only-main.md', 'main-only source\n')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.project.git('add', '--', 'docs/only-main.md')
        self.project.git('commit', '-qm', 'main source')
        card = self.create_card('only-main.md', source)
        self.project.git('checkout', '-q', 'feature')
        with patch.object(workspace, 'recheck'), patch.object(workspace, 'request_sync', return_value=True):
            result = retire.retire_deleted_source(self.root, expanded(self.project), self.bound(),
                                                  self.project.raw_name, 'docs/only-main.md', digest)
        self.assertEqual((result['state'], result['reason']), ('held_branch', 'source_on_other_branch'))
        self.assertTrue(card.exists())

    def test_never_tracked_source_retires_only_a_committed_card(self):
        source1 = self.project.write_note('docs/scratch.md', 'local scratch\n')
        source2 = self.project.write_note('docs/scratch2.md', 'other scratch\n')
        committed = self.create_card('committed.md', source1)
        self.project.git('add', '--', '.memvane/wiki/concepts/committed.md')
        self.project.git('commit', '-qm', 'card only')
        loose = self.create_card('loose.md', source2)
        source1.unlink()
        source2.unlink()
        result = self.run_selected()
        self.assertFalse(committed.exists())
        self.assertTrue(loose.exists())
        self.assertEqual((result['retired'], result['held']), (1, 1))

    def test_dry_run_reports_held_cards_without_writing(self):
        self.project.git('branch', 'feature')
        source = self.project.write_note('docs/only-main.md', 'main-only source\n')
        self.project.git('add', '--', 'docs/only-main.md')
        self.project.git('commit', '-qm', 'main source')
        card = self.create_card('only-main.md', source)
        self.project.git('checkout', '-q', 'feature')
        before = card.read_bytes()
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual(report['counts']['missing'], 1)
        self.assertEqual(report['held'], 1)
        self.assertEqual(report['heldReasons'], {'source_on_other_branch': 1})
        self.assertEqual(report['guard']['wouldDelete'], 0)
        self.assertEqual(card.read_bytes(), before)
