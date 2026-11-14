import unittest
from tests.support.case import FixtureCase
from memvane.route import api
from memvane.route import types


class RouteValueTests(FixtureCase):
    def test_route_state_decisions_have_explicit_state_and_reason(self):
        self.assertEqual(api.decision('blocked', 'route_settings_changed'),
                         {'state': 'blocked', 'reason': 'route_settings_changed'})

    def test_pair_parser_rejects_duplicate_json_keys(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_route_key'):
            api.pairs([('name', 'a'), ('name', 'b')])

    def test_route_collection_roots_keep_only_the_permitted_wiki_overlap(self):
        from pathlib import Path
        project = self.tmp / 'project'
        wiki = project / '.memvane' / 'wiki'
        wiki.mkdir(parents=True)
        docs = project / 'docs'
        docs.mkdir()
        rows = [{'name': 'raw', 'sourceRoot': str(project), 'role': 'raw'},
                {'name': 'wiki', 'sourceRoot': str(wiki), 'role': 'wiki'}]
        self.assertEqual(api.selected_collection_roots(project, rows),
                         {'raw': str(project), 'wiki': str(wiki)})
        rows[1] = {'name': 'nested', 'sourceRoot': str(docs), 'role': 'wiki'}
        with self.assertRaisesRegex(ValueError, 'selected_collection_roots_overlap'):
            api.selected_collection_roots(project, rows)

    def test_route_record_constants_are_the_v1_schema(self):
        self.assertEqual(types.SCHEMA_V4, 'memvane-route-v4')
        self.assertIn('vectorProfile', types.FIELDS_V4)

    def test_current_flat_route_settings_are_supported(self):
        raw = {'recallStrategy': 'flat', 'collectionRoles': {'docs': 'raw'}, 'topN': 3}
        cfg = {'recallStrategy': 'flat', 'collectionRoles': {'docs': 'raw'},
               'topN': 3, 'skipPaths': [], 'lexicalPatterns': [], 'minScore': 0,
               'rawFallbackMinScore': 0}
        self.assertTrue(api.selected_settings_supported(raw, cfg, 'docs'))
        self.assertFalse(api.selected_settings_supported({**raw, 'topN': 6}, cfg, 'docs'))

    def test_foreign_format_is_classified_without_guessing(self):
        value = api.foreign_format({'schema': 'unknown-schema'})
        self.assertEqual((value['state'], value['reason']),
                         ('blocked', 'route_major_mismatch'))
        self.assertTrue(value['recallQuiet'])

    def test_absolute_path_requires_canonical_spelling(self):
        self.assertTrue(api.canonical_absolute(str(self.tmp / 'a')))
        self.assertFalse(api.canonical_absolute('relative/path'))


if __name__ == '__main__':
    unittest.main()
