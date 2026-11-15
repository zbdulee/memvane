import unittest
from pathlib import Path
from tests.support.case import FixtureCase
from tests.support.project import make_project
from memvane.settings import api


class CollectionMatchTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, name='Story')

    def choose(self, paths, config):
        return api.select_collections(paths, self.project.root, config)

    def test_longest_prefix_selects_nested_collection(self):
        cfg = {'collections': ['outer', 'inner'],
               'collectionPaths': {'outer': 'docs', 'inner': 'docs/sub'}}
        self.assertEqual(self.choose(['docs/sub/chapter.md'], cfg),
                         {'inner': str(self.project.root / 'docs' / 'sub')})

    def test_distinct_collections_are_selected_for_distinct_edits(self):
        cfg = {'collections': ['manuscript', 'plot'],
               'collectionPaths': {'manuscript': 'manuscript', 'plot': 'plot'}}
        self.assertEqual(self.choose(['manuscript/a.md', 'plot/b.md'], cfg),
                         {'manuscript': str(self.project.root / 'manuscript'),
                          'plot': str(self.project.root / 'plot')})

    def test_edit_outside_collection_selects_nothing(self):
        cfg = {'collections': ['docs'], 'collectionPaths': {'docs': 'docs'}}
        self.assertEqual(self.choose(['README.md'], cfg), {})

    def test_home_path_expands_before_allow_root_comparison(self):
        docs = self.home / 'Documents'
        docs.mkdir()
        cfg = {'collections': ['home-docs'],
               'collectionPaths': {'home-docs': '~/Documents'},
               'allowRoots': ['~/Documents']}
        self.assertEqual(self.choose([str(docs / 'note.md')], cfg),
                         {'home-docs': str(docs)})

    def test_wildcard_keys_return_actual_collection_names(self):
        cfg = {'collections': ['story-manuscript', 'story-plot'],
               'collectionPaths': {'*-manuscript': '04_Manuscript', '*-plot': '03_Plot'}}
        result = self.choose(['04_Manuscript/a.md', '03_Plot/b.md'], cfg)
        self.assertEqual(set(result), {'story-manuscript', 'story-plot'})

    def test_exact_pattern_precedes_later_wildcard(self):
        cfg = {'collections': ['story-manuscript'],
               'collectionPaths': {'story-manuscript': 'exact', '*-manuscript': 'fallback'}}
        self.assertEqual(self.choose(['exact/chapter.md'], cfg),
                         {'story-manuscript': str(self.project.root / 'exact')})

    def test_unmapped_and_empty_collection_lists_select_nothing(self):
        self.assertEqual(self.choose(['docs/a.md'], {'collections': ['docs']}), {})
        self.assertEqual(self.choose(['docs/a.md'], {
            'collectionPaths': {'docs': 'docs'}}), {})

    def test_paths_outside_allowed_roots_are_never_selected(self):
        outside = self.tmp / 'private'
        outside.mkdir()
        cfg = {'collections': ['private'], 'collectionPaths': {'private': str(outside)}}
        self.assertEqual(self.choose([str(outside / 'note.md')], cfg), {})

    def test_outside_path_is_excluded_unless_allow_rooted(self):
        outside = self.tmp / 'shared'
        outside.mkdir()
        path = outside / 'note.md'
        cfg = {'collections': ['shared'], 'collectionPaths': {'shared': str(outside)}}
        self.assertEqual(self.choose([str(path)], cfg), {})
        cfg['allowRoots'] = [str(outside)]
        self.assertEqual(self.choose([str(path)], cfg), {'shared': str(outside)})
