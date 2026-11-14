"""Selected format-4 retrieval combines published and live wiki candidates."""
from unittest.mock import patch

from memvane.wiki.compile import selected_execution as execution, wiki_compile_retrieval as retrieval
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class PublishedRetrieval(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        source = self.project.write_note('docs/source.md', 'Synthetic archive source.\n')
        self.card = self.project.write_card('concepts/archive.md', title='Archive',
                                            body='Archives retain spacecraft records.\n',
                                            sources=[source])
        self.project.git('add', '--', '.memvane/settings.json', '.memvane/wiki', 'docs')
        self.project.git('commit', '-qm', 'fixture')
        self.install = build_install(self, self.project)
        self.bound = workspace.selected(self.project.root)

    def matches(self, text):
        return execution.wiki_matches(self.project.root, self.project.wiki, text, 20, 12000,
                                      bound=self.bound)

    def test_published_lexical_search_survives_cold_provider_and_live_overlay(self):
        with patch.object(retrieval, 'overlay_paths', return_value=[]):
            with retrieval.selected_search(self.bound, 'archive') as (rows, _exclusions, _name, mode):
                self.assertEqual(mode, 'bound')
                self.assertIn('concepts/archive.md', [row[0] for row in rows])
            self.assertIn(self.card, [row[0] for row in self.matches('archive')])
        new = self.project.write_card('concepts/new.md', title='Quasar',
                                       body='Quasar observatory record.\n')
        self.assertIn(new, [row[0] for row in self.matches('quasar observatory')])

    def test_warm_provider_uses_same_published_head(self):
        with patch.object(retrieval, 'overlay_paths', return_value=[]), \
             patch.object(retrieval, 'query_provider', return_value=lambda _text, _profile: {'vector': [1.0, 0.0]}) as query:
            self.assertIn(self.card, [row[0] for row in self.matches('archive')])
        query.assert_called_once()
