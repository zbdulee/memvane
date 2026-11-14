import copy
import json
import unittest
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from memvane.route import api
from memvane.journal import api as journal


class SelectedRouteTests(FixtureCase):
    def test_route_lookup_blocks_corrupt_nested_and_symlink_markers(self):
        project = make_project(self.home, git=False)
        nested = project.root / 'docs' / 'nested'
        nested.mkdir(parents=True, exist_ok=True)
        marker = project.root / '.memvane' / 'route.json'
        self.assertEqual(journal.select_route(str(project.root), linked=False)['state'], 'legacy')
        marker.write_text('{', encoding='utf-8')
        self.assertEqual(journal.select_route(str(project.root), linked=False)['state'], 'blocked')
        marker.write_text('{}', encoding='utf-8')
        self.assertEqual(journal.select_route(str(project.root), linked=False)['state'], 'blocked')
        marker.unlink()
        target = self.tmp / 'target-route.json'
        target.write_text('{}', encoding='utf-8')
        marker.symlink_to(target)
        self.assertEqual(journal.select_route(str(project.root), linked=False)['state'], 'blocked')
        marker.unlink()
        marker.write_text('{}', encoding='utf-8')
        (nested / '.memvane').mkdir()
        (nested / '.memvane' / 'route.json').write_text('{}', encoding='utf-8')
        result = journal.select_route(str(nested), linked=False)
        self.assertEqual((result['state'], result['reason']), ('blocked', 'ambiguous_route_records'))
        self.assertEqual(journal.select_route(str(project.root), action='unsupported', linked=False)['reason'],
                         'unsupported_route_action')

    def test_selected_install_record_classifies_from_its_current_proof(self):
        project = make_project(self.home, name='Route fixture')
        install = build_install(self, project)
        record_path = install.state / 'route.json'
        record = json.loads(record_path.read_text(encoding='utf-8'))
        decision = api.classify_v4(project.root, project.settings_path, record)
        self.assertEqual(decision['state'], 'candidate')
        self.assertEqual(decision['installId'], install.install_id)
        for action in ('recall', 'update', 'update-queued', 'gate', 'posttool',
                       'index', 'compile', 'file-change-event'):
            with self.subTest(action=action):
                selected = journal.select_route(str(project.root), action=action, linked=False)
                self.assertEqual(selected['state'], 'candidate', selected)
                self.assertEqual(selected['installId'], install.install_id)

        for field, bad_value, reason in (
            ('projectRoot', str(self.tmp / 'other'), 'route_project_mismatch'),
            ('settingsSha256', '0' * 64, 'route_settings_changed'),
            ('stateNode', ['bad', 1], 'invalid_route_node'),
            ('status', 'pending', 'invalid_route_fields'),
        ):
            with self.subTest(field=field):
                modified = copy.deepcopy(record)
                modified[field] = bad_value
                with self.assertRaisesRegex(ValueError, reason):
                    api.classify_v4(project.root, project.settings_path, modified)

        foreign = copy.deepcopy(record)
        foreign['stateRoot'] = str(self.tmp / 'foreign')
        self.assertEqual(api.classify_v4(project.root, project.settings_path, foreign)['reason'],
                         'foreign_install')

        for field, bad in (('dimension', 0), ('dimension', True), ('metric', 'euclidean'),
                           ('embeddingFingerprint', 'bad'), ('providerRelativePath', '../escape.py'),
                           ('approvedProviderId', 'bad/name')):
            with self.subTest(vector_field=field, value=bad):
                modified = copy.deepcopy(record)
                modified['vectorProfile'][field] = bad
                with self.assertRaisesRegex(ValueError, 'invalid_vector_profile'):
                    api.classify_v4(project.root, project.settings_path, modified)

        modified = copy.deepcopy(record)
        modified['collections'][0]['name'] = 'unexpected'
        with self.assertRaisesRegex(ValueError, 'route_settings_changed'):
            api.classify_v4(project.root, project.settings_path, modified)

        original_settings = project.settings_path.read_bytes()
        try:
            project.settings_path.write_text('{"name":"changed"}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'route_settings_changed'):
                api.classify_v4(project.root, project.settings_path, record)
        finally:
            project.settings_path.write_bytes(original_settings)


if __name__ == '__main__':
    unittest.main()
