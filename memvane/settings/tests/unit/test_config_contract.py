import unittest
from tests.support.case import FixtureCase
from tests.support.project import make_project
from memvane.settings import api


class ConfigContractTests(unittest.TestCase):
    def test_minimal_settings_expands_with_raw_and_wiki_roles(self):
        cfg = api.normalize_config({'name': 'Demo', 'skipPaths': ['drafts/']}, '.memvane')
        self.assertEqual(cfg['collectionRoles'], {'demo': 'raw', 'demo-wiki': 'wiki'})
        self.assertEqual(cfg['skipPaths'], ['drafts/'])
        self.assertEqual(cfg['wikiPath'], '.memvane/wiki')

    def test_unknown_collection_role_is_fail_closed(self):
        cfg = api.normalize_config({'name': 'Demo', 'collectionRoles': {'demo': 'mystery'}})
        self.assertEqual(api.collection_role(api.role_map(cfg), 'demo'), 'raw')
        invalid = api.normalize_config({'name': 'Demo', 'collectionRoles': {'demo': 'mystery'}})
        self.assertNotIn('demo', invalid['collectionRoles'])

    def test_numeric_values_are_coerced_and_clamped(self):
        cfg = api.normalize_config({'name': 'Demo', 'topN': '7', 'queryTimeout': 'bad'})
        self.assertEqual(cfg['topN'], 7)
        self.assertEqual(cfg['queryTimeout'], 5.0)
        budget = api.normalize_config({'compile': {'budget': {'extractorPerRun': 999999}}})['compile']['budget']
        self.assertEqual(budget['extractorPerRun'], 50)
        for value in (True, False, 1.9, 2.5):
            with self.subTest(value=value):
                budget = api.normalize_config({'compile': {'budget': {'extractorPerRun': value}}})['compile']['budget']
                self.assertEqual(budget['extractorPerRun'], 10)

    def test_diagnostics_requires_boolean_true(self):
        # Consent also requires a proved selected/preview route and an exact
        # settings file, so an incomplete decision always fails closed.
        for decision in ({}, {'state': 'candidate'}, {'state': 'blocked'}):
            self.assertFalse(api.diagnostic_enabled(decision))


class ConfigFileTests(FixtureCase):
    def test_malformed_project_settings_fail_open_to_empty_config(self):
        project = make_project(self.home, name='Broken config')
        project.settings_path.write_text('{broken', encoding='utf-8')
        found = api.find_project_config(str(project.root))
        self.assertEqual(found['configFormat'], 'none')
        self.assertIsNone(found['configPath'])
        self.assertEqual(found['config']['collections'], [])
