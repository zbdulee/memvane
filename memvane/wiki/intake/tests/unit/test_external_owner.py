"""External edits select a unique longest raw-root component prefix."""
from pathlib import Path

from memvane.wiki.intake import external_edits
from tests.support.case import FixtureCase


class OwnerTieBreak(FixtureCase):
    def owner(self, path, *roots):
        targets = [{'projectRoot': root, 'rawRoot': root} for root in roots]
        chosen = external_edits.owner_for(Path(path), targets)
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


class SourceEligibility(FixtureCase):
    def setUp(self):
        super().setUp()
        from tests.support.project import make_project
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.decision = {'projectRoot': str(self.root), 'recallSettings': {'skipPaths': ['build']}}

    def test_hidden_denied_and_skipped_paths_are_ineligible(self):
        for relative in ('.memvane/wiki/card.md', '.git/config.md', '.draft/note.md',
                         'node_modules/package.md', 'build/result.md', 'code.py'):
            with self.subTest(path=relative):
                self.assertFalse(external_edits._eligible(self.root / relative, self.decision))
        self.assertTrue(external_edits._eligible(self.root / 'docs/note.md', self.decision))
        self.assertFalse(external_edits._eligible(self.home / 'outside.md', self.decision))

    def test_symlinked_component_is_rejected_before_routing(self):
        outside = self.home / 'outside'
        outside.mkdir()
        (self.root / 'link').symlink_to(outside, target_is_directory=True)
        self.assertIsNone(external_edits._safe_path('link/note.md', self.root))
        self.assertEqual(external_edits._safe_path('docs/note.md', self.root),
                         self.root / 'docs/note.md')

    def test_linked_worktree_marker_excludes_source(self):
        nested = self.root / 'linked'
        nested.mkdir()
        (nested / '.git').write_text('gitdir: synthetic\n')
        self.assertFalse(external_edits._eligible(nested / 'note.md', self.decision))
