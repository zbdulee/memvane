"""Live wiki overlay respects selected source and scaffold boundaries."""
from contextlib import contextmanager
import hashlib
import json
from unittest.mock import patch

from memvane.wiki.compile import selected_execution as execution, wiki_compile_retrieval as retrieval
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project


class SelectedRetrieval(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.wiki = self.project.wiki
        decision = {'state': 'candidate', 'projectRoot': str(self.project.root),
                    'installId': 'a' * 32, 'scopeId': 'c' * 32, 'storeIncarnation': 'd' * 32,
                    'collections': [{'name': self.project.wiki_name, 'role': 'wiki',
                                     'sourceRoot': str(self.wiki)}],
                    'recallSettings': {'skipPaths': []}}
        self.bound = (decision, {'productRouteAllowed': True})
        @contextmanager
        def no_published_index(*args):
            yield [], (frozenset(), frozenset()), self.project.wiki_name, 'bound'
        for item in (patch.object(execution, 'recheck'), patch.object(workspace, 'recheck'),
                     patch.object(retrieval, 'selected_search', no_published_index)):
            item.start()
            self.addCleanup(item.stop)

    def matches(self):
        return execution.wiki_matches(self.project.root, self.wiki, 'starship archive',
                                      10, 100, bound=self.bound)

    def test_unpublished_live_card_is_retrieved(self):
        (self.wiki / 'one.md').write_text('# Mirror\nA starship archive')
        (self.wiki / 'two.md').write_text('# Other\nA garden')
        rows = self.matches()
        self.assertEqual([row[0].name for row in rows], ['one.md'])
        self.assertGreater(rows[0][1], 0)

    def test_root_scaffolding_is_ignored_but_nested_index_is_a_card(self):
        for name in ('SCHEMA.md', 'index.md', 'log.md'):
            (self.wiki / name).write_text('# Meta\nstarship archive')
        nested = self.wiki / 'concepts/index.md'
        nested.parent.mkdir(exist_ok=True)
        nested.write_text('# Nested\nstarship archive')
        self.assertEqual([row[0].relative_to(self.wiki).as_posix() for row in self.matches()],
                         ['concepts/index.md'])

    def test_mixed_card_with_excluded_source_is_not_retrieved(self):
        def revision(path):
            return {'kind': 'file', 'path': path, 'collection': 'raw',
                    'sha256': hashlib.sha256(path.encode()).hexdigest(), 'size': len(path), 'mtimeNs': 1}
        card = ('---\nsourceRevisions:\n  - ' + json.dumps(revision('docs/keep/a.md'))
                + '\n  - ' + json.dumps(revision('docs/build/b.md'))
                + '\n---\n# Card\nstarship archive\n')
        (self.wiki / 'mixed.md').write_text(card)
        self.bound[0]['recallSettings']['skipPaths'] = ['build']
        self.assertEqual(self.matches(), [])
