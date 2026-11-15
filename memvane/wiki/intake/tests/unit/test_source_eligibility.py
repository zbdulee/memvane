"""Automatic compile source eligibility keeps role and path boundaries."""
from memvane.wiki.intake import wiki_compile_enqueue as enqueue
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class SourceEligibility(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.config = expanded(self.project)

    def config_for(self, root, role='raw'):
        cfg = dict(self.config)
        cfg['collections'] = ['source-collection']
        cfg['collectionPaths'] = {'source-collection': root}
        cfg['collectionRoles'] = {'source-collection': role}
        return cfg

    def record(self, rel, config):
        path = self.project.write_note(rel, 'synthetic text\n')
        return enqueue._source_record(str(path), self.project.root, self.project.root, config, 'codex')

    def test_only_source_roles_and_text_extensions_are_eligible(self):
        for role in ('raw', 'session', 'source', 'wiki'):
            with self.subTest(role=role):
                record = self.record('docs/guide.md', self.config_for('docs', role))
                if role == 'wiki':
                    self.assertIsNone(record)
                else:
                    self.assertEqual(record['source'], {'kind': 'file', 'path': 'docs/guide.md',
                                                        'collection': 'source-collection'})
                    self.assertEqual(record['engine'], 'codex')
                    self.assertNotIn('content', record['source'])
        for rel, allowed in (('docs/note.txt', True), ('docs/guide.RST', True),
                             ('docs/tool.py', False), ('docs/node_modules/pkg/readme.md', False)):
            with self.subTest(path=rel):
                self.assertEqual(self.record(rel, self.config_for('docs')) is not None, allowed)

    def test_dot_root_allows_registered_root_but_not_hidden_descendants(self):
        config = self.config_for('.nova/06_Sessions', 'session')
        cases = (('.nova/06_Sessions/entry.md', True),
                 ('.nova/06_Sessions/archive/old.md', True),
                 ('.nova/06_Sessions/.drafts/wip.md', False),
                 ('.nova/06_Sessions/.hidden.md', False), ('.claude/agent.md', False))
        for rel, allowed in cases:
            with self.subTest(path=rel):
                self.assertEqual(self.record(rel, config) is not None, allowed)

    def test_denied_segments_stay_denied_even_as_collection_roots(self):
        for root in ('.memvane', '.git', 'node_modules'):
            with self.subTest(root=root):
                self.assertIsNone(self.record(root + '/not-source.md', self.config_for(root)))

    def test_repository_root_does_not_exempt_hidden_descendants(self):
        config = self.config_for('.')
        for rel, allowed in (('docs/plain.md', True), ('.nova/note.md', False),
                             ('docs/.draft/note.md', False), ('docs/.hidden.md', False)):
            with self.subTest(path=rel):
                self.assertEqual(self.record(rel, config) is not None, allowed)
