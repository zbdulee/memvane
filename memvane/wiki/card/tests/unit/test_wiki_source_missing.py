import unittest
from tests.support.case import FixtureCase
from memvane.wiki.card import api


class WikiSourceMissingTests(FixtureCase):
    def test_missing_source_keys_are_reported_without_partial_parse(self):
        entries, error = api.declared_source_records('sources: [{path: "a.md"}, {bad: 1}]')
        self.assertIsNotNone(error)
        self.assertEqual(entries, [])

    def test_source_classification_distinguishes_missing_from_existing(self):
        from pathlib import Path
        root = self.tmp / 'project'
        (root / '.memvane' / 'wiki').mkdir(parents=True)
        self.assertEqual(api.wiki_root_of(root, {'wikiPath': '.memvane/wiki'}),
                         root / '.memvane' / 'wiki')

    def test_source_root_cannot_escape_the_project(self):
        root = self.tmp / 'project'
        root.mkdir()
        self.assertIsNone(api.wiki_root_of(root, {'wikiPath': '../outside'}))


if __name__ == '__main__':
    unittest.main()
