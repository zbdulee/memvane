import json
from tests.support.case import FixtureCase
from tests.support.project import make_project
from memvane.settings import api as settings


class DiagnosticConsentTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.route = {'state': 'candidate', 'projectRoot': str(self.project.root),
                      'installId': 'fixture-install', 'journalPath': str(self.tmp / 'journal.sqlite')}

    def settings(self, value):
        self.project.settings_path.write_text(json.dumps({'name': 'fixture', 'diagnostics': value}),
                                              encoding='utf-8')

    def test_consent_is_reread_and_requires_boolean_true(self):
        for value, expected in (({'enabled': True}, True), ({'enabled': False}, False),
                                ({'enabled': 'true'}, False), ({'enabled': 1}, False),
                                (None, False), ({'enabled': True}, True)):
            with self.subTest(value=value):
                self.settings(value)
                self.assertIs(settings.diagnostic_enabled(self.route), expected)
        self.assertNotIn('diagnostics', self.route)

    def test_unproved_route_and_malformed_settings_are_off(self):
        self.settings({'enabled': True})
        for state in ('blocked', 'legacy', 'stale', None):
            with self.subTest(state=state):
                self.assertFalse(settings.diagnostic_enabled({**self.route, 'state': state}))
        for route in (None, {}, {**self.route, 'projectRoot': 'relative'},
                      {**self.route, 'installId': ''}, {**self.route, 'journalPath': 'relative'}):
            with self.subTest(route=route):
                self.assertFalse(settings.diagnostic_enabled(route))
        self.project.settings_path.write_text('{broken', encoding='utf-8')
        self.assertFalse(settings.diagnostic_enabled(self.route))

    def test_diagnostics_only_changes_its_own_effective_configuration(self):
        without = settings.normalize_config({'name': 'fixture'})
        with_diagnostics = settings.normalize_config({'name': 'fixture', 'diagnostics': {'enabled': True}})
        self.assertFalse(without.pop('diagnostics')['enabled'])
        self.assertTrue(with_diagnostics.pop('diagnostics')['enabled'])
        self.assertEqual(with_diagnostics, without)

    def test_missing_or_unreadable_settings_fail_closed(self):
        from unittest.mock import patch
        self.settings({'enabled': True})
        self.project.settings_path.unlink()
        self.assertFalse(settings.diagnostic_enabled(self.route))
        self.settings({'enabled': True})
        with patch('builtins.open', side_effect=PermissionError('denied')):
            self.assertFalse(settings.diagnostic_enabled(self.route))
        self.assertFalse(settings.diagnostic_enabled({**self.route, 'projectRoot': str(self.tmp / 'missing')}))

    def test_linked_worktree_uses_only_proved_main_settings(self):
        linked = make_project(self.home, git=False, dirname='linked')
        linked.settings_path.write_text(json.dumps({'name': 'linked', 'diagnostics': {'enabled': True}}))
        route = {'state': 'worktree', 'reason': 'linked_worktree_read_only',
                 'worktreeRoot': str(linked.root), 'worktreeProjectRoot': str(linked.root),
                 'mainRoute': self.route}
        self.settings({'enabled': False})
        self.assertFalse(settings.diagnostic_enabled(route))
        self.settings({'enabled': True})
        self.assertTrue(settings.diagnostic_enabled(route))
        self.assertFalse(settings.diagnostic_enabled({**route, 'mainRoute': {'state': 'blocked'}}))
        self.assertFalse(settings.diagnostic_enabled({**route, 'reason': 'other'}))
