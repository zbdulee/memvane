"""Selected format-4 retrieval combines published and live wiki candidates."""
from unittest.mock import patch

from memvane.learning import api as learning
from memvane.store import api as store
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
        self.korean = self.project.write_card('concepts/korean.md', title='기록',
                                              body='검마의 기록.\n', sources=[source])
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

    def test_admitted_fixture_provider_queries_published_vectors(self):
        self.install.provider.admit_queries()
        with patch.object(retrieval, 'overlay_paths', return_value=[]):
            self.assertIn(self.card, [row[0] for row in self.matches('archive')])

    def test_warm_provider_uses_same_published_head(self):
        with patch.object(retrieval, 'overlay_paths', return_value=[]), \
             patch.object(retrieval, 'query_provider', return_value=lambda _text, _profile: {'vector': [1.0, 0.0]}) as query:
            self.assertIn(self.card, [row[0] for row in self.matches('archive')])
        query.assert_called_once()

    def test_korean_particle_and_english_stem_use_published_lexical_rows(self):
        with patch.object(retrieval, 'overlay_paths', return_value=[]):
            self.assertIn(self.korean, [row[0] for row in self.matches('검마는')])
            self.assertIn(self.card, [row[0] for row in self.matches('archive')])

    def test_query_does_not_enter_learning_and_failed_vector_read_releases_pin(self):
        with patch.object(retrieval, 'overlay_paths', return_value=[]), \
             patch.object(learning, 'select_published', side_effect=AssertionError('learning called')), \
             patch.object(learning, 'capture_recall', side_effect=AssertionError('learning called')):
            self.assertIn(self.card, [row[0] for row in self.matches('archive')])
            with patch.object(store, 'pinned_vector', side_effect=RuntimeError('synthetic read failure')):
                with self.assertRaisesRegex(RuntimeError, 'synthetic read failure'):
                    self.matches('archive')
            self.assertIn(self.card, [row[0] for row in self.matches('archive')])


class WeakPublishedVectors(FixtureCase):
    def test_twenty_weak_real_vector_neighbors_do_not_fill_result_quota(self):
        project = make_project(self.home)
        source = project.write_note('docs/source.md', 'Synthetic source.\n')
        for number in range(20):
            project.write_card('concepts/card-%02d.md' % number, title='Garden %d' % number,
                               body='Garden flower number %d.\n' % number, sources=[source])
        project.git('add', '--', '.memvane/settings.json', '.memvane/wiki', 'docs')
        project.git('commit', '-qm', 'fixture')
        build_install(self, project)
        bound = workspace.selected(project.root)
        with patch.object(retrieval, 'overlay_paths', return_value=[]), \
             patch.object(retrieval, 'query_provider', return_value=lambda _text, _profile: {'vector': [-1.0, 0.0]}):
            with retrieval.selected_search(bound, 'unmentionedxyz') as (rows, _excluded, _name, _mode):
                self.assertEqual(len(rows), 20)
                self.assertTrue(all(row[2] == -1.0 for row in rows))
            result = execution.wiki_matches(project.root, project.wiki, 'unmentionedxyz', 20, 12000,
                                            bound=bound)
            self.assertEqual(result, [])
