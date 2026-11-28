"""Query Playground over a real selected install proved through its registry row.

The install is built and selected through the install use cases and
registered the way setup registers it (``registry.api.after_setup``). The
playground runs ``recall.api.dry_run_recall`` on the proved checkout: the
response keeps the ``DryRunRecallResponse`` shape, writes no event line and
never asks or starts a cold selector.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

from memvane.registry import api as registry
from memvane.ui import recall_adapters
from memvane.ui.read_adapters import TargetError
from tests.support.case import FixtureCase

NOTE = 'Field notes: the emerald heron nests beside the reed pond.\n'
PROMPT = 'what marker does the heron protocol keep?'
TOP_KEYS = {'route', 'linked', 'outcome', 'reason', 'generation', 'lexicalLists', 'semanticList',
            'fusedList', 'selectorResult', 'semanticStatus', 'vectorCoveredMembers',
            'vectorEligibleMembers', 'fallbackReason', 'notes', 'timings'}


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and not path.is_symlink() and not path.name.endswith(('-wal', '-shm', '-journal')):
            digest.update(str(path).encode() + path.read_bytes())
    return digest.hexdigest()


class QueryPlaygroundTests(FixtureCase):
    def setUp(self):
        super().setUp()
        from memvane.kernel import hook_budget
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        budget = mock.patch.object(hook_budget, '_deadline', None)
        budget.start()
        self.addCleanup(budget.stop)
        self.project = make_project(self.home, name='recallui', dirname='recallui')
        note = self.project.write_note('notes.md', NOTE)
        for topic in ('heron', 'glacier'):
            self.project.write_card(
                f'concepts/{topic}.md', title=topic.title() + ' protocol', sources=[note],
                body=f'The {topic} protocol keeps marker token zq{topic}x and zqsharedx.\n')
        self.project.commit('fixture')
        self.install = build_install(self, self.project, install_id='e' * 32)
        self.install.settle()
        outcome = registry.after_setup('activate-index-origin', str(self.project.root), str(self.install.state),
                                       {'status': 'selected', 'phase': 'selected'})
        self.assertIn(outcome['status'], ('registered', 'unchanged'), outcome)
        self.id = self.install.install_id
        self.wakes = []
        wake = mock.patch('memvane.runtime.api.request',
                          side_effect=lambda *args, **kwargs: self.wakes.append(args) or {'status': 'queued'})
        wake.start()
        self.addCleanup(wake.stop)

    def run_playground(self, body):
        """One playground call; asserts it wrote no event line, state file or wake."""
        events = self.install.state / 'events.jsonl'
        raw = events.read_bytes() if events.exists() else None
        before = tree_digest(self.install.state)
        view = recall_adapters.query_playground(self.id, body, home=self.home)
        self.assertEqual(events.read_bytes() if events.exists() else None, raw)
        self.assertEqual(tree_digest(self.install.state), before)
        self.assertEqual(self.wakes, [])
        json.dumps(view, allow_nan=False)
        return view

    def assert_schema(self, view):
        self.assertEqual(set(view), TOP_KEYS)
        self.assertIsInstance(view['route'], str)
        self.assertIsInstance(view['linked'], bool)
        self.assertIn(view['outcome'], ('served', 'empty', 'refused'))
        self.assertIsInstance(view['reason'], str)
        self.assertTrue(view['generation'] is None or isinstance(view['generation'], str))
        for lane in view['lexicalLists']:
            self.assertEqual(set(lane), {'query', 'rows'})
            self.assertIsInstance(lane['query'], str)
            for row in lane['rows']:
                self.assertEqual(set(row), {'path', 'blob', 'score', 'rank'})
        if view['semanticList'] is not None:
            for row in view['semanticList']:
                self.assertEqual(set(row), {'path', 'blob', 'cosine', 'rank'})
        for row in view['fusedList']:
            self.assertEqual(set(row), {'path', 'blob', 'rrfScore', 'rank'})
            self.assertIsInstance(row['blob'], str)
        selector = view['selectorResult']
        self.assertEqual(set(selector), {'ran', 'reason', 'checkpoint', 'final', 'decisions'})
        self.assertIsInstance(selector['ran'], bool)
        for card in selector['final']:
            self.assertEqual(set(card), {'path', 'sha'})
        for decision in selector['decisions']:
            self.assertEqual(set(decision), {'path', 'picked', 'reason'})
            self.assertIsInstance(decision['picked'], bool)
        for key in ('vectorCoveredMembers', 'vectorEligibleMembers'):
            self.assertTrue(view[key] is None or type(view[key]) is int)
        self.assertTrue(all(isinstance(note, str) for note in view['notes']))
        self.assertTrue(all(type(value) in (int, float) for value in view['timings'].values()))

    def test_a_dry_run_keeps_the_response_shape_and_writes_nothing(self):
        self.install.provider.admit_queries()
        view = self.run_playground({'prompt': PROMPT})
        self.assert_schema(view)
        self.assertEqual((view['route'], view['linked'], view['outcome']), ('candidate', False, 'served'))
        self.assertTrue(view['lexicalLists'])
        self.assertTrue(view['fusedList'])
        self.assertTrue(any(row['path'].endswith('heron.md') for row in view['fusedList']))
        self.assertEqual([row['rank'] for row in view['fusedList']],
                         list(range(1, len(view['fusedList']) + 1)))
        selector = view['selectorResult']
        self.assertEqual((selector['ran'], selector['reason']), (False, 'selector_disabled'))
        picked = [row['path'] for row in selector['decisions'] if row['picked']]
        self.assertEqual(picked, [card['path'] for card in selector['final']])
        self.assertTrue(selector['final'])
        self.assertIn('totalMs', view['timings'])
        self.assertNotIn('zqheronx', json.dumps(view))

    def test_semantic_off_reports_its_fallback(self):
        view = self.run_playground({'prompt': PROMPT, 'useSemantic': False, 'timeoutMs': 15000})
        self.assert_schema(view)
        self.assertEqual((view['outcome'], view['semanticStatus'], view['fallbackReason']),
                         ('served', 'provider_absent', 'semantic_disabled'))
        self.assertIsNone(view['semanticList'])

    def test_a_cold_selector_is_never_asked_or_started(self):
        from memvane.runtime import api as runtime_api
        refused = mock.Mock(side_effect=AssertionError('selector asked'))
        start = mock.Mock(side_effect=AssertionError('service start'))
        created = []
        real_root = runtime_api.service_root

        def service_root(**kwargs):
            created.append(kwargs.get('create', False))
            return real_root(**kwargs)
        with mock.patch('memvane.learning.api.select_published', refused), \
                mock.patch('memvane.resident.api.ensure_service', start), \
                mock.patch('memvane.runtime.api.service_root', side_effect=service_root):
            view = self.run_playground({'prompt': PROMPT, 'useSelector': True})
            with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                    mock.patch('memvane.learning.api.inference_version', return_value=None):
                warm_less = self.run_playground({'prompt': PROMPT, 'useSelector': True})
        self.assert_schema(view)
        self.assertEqual(view['outcome'], 'served')
        self.assertFalse(view['selectorResult']['ran'])
        self.assertIn(view['selectorResult']['reason'], ('selector_not_prepared', 'selector_not_warm'))
        self.assertEqual((warm_less['selectorResult']['ran'], warm_less['selectorResult']['reason']),
                         (False, 'selector_not_warm'))
        self.assertTrue(warm_less['selectorResult']['final'])
        refused.assert_not_called()
        start.assert_not_called()
        self.assertNotIn(True, created)

    def test_invalid_input_is_refused_before_any_read(self):
        dry_run = mock.Mock(side_effect=AssertionError('dry run'))
        bad = (
            (None, 'body_invalid'),
            ([], 'body_invalid'),
            ({}, 'prompt_invalid'),
            ({'prompt': '   '}, 'prompt_invalid'),
            ({'prompt': 3}, 'prompt_invalid'),
            ({'prompt': 'x' * (recall_adapters.MAX_PROMPT_CHARS + 1)}, 'prompt_too_long'),
            ({'prompt': PROMPT, 'useSemantic': 'yes'}, 'useSemantic_invalid'),
            ({'prompt': PROMPT, 'useSelector': 1}, 'useSelector_invalid'),
            ({'prompt': PROMPT, 'timeoutMs': 199}, 'timeoutMs_invalid'),
            ({'prompt': PROMPT, 'timeoutMs': 15001}, 'timeoutMs_invalid'),
            ({'prompt': PROMPT, 'timeoutMs': 1000.0}, 'timeoutMs_invalid'),
            ({'prompt': PROMPT, 'timeoutMs': True}, 'timeoutMs_invalid'),
            ({'prompt': PROMPT, 'extra': 1}, 'body_unknown_field'),
        )
        with mock.patch('memvane.recall.api.dry_run_recall', dry_run):
            for body, code in bad:
                with self.subTest(code=code, body=str(body)[:40]):
                    with self.assertRaises(TargetError) as caught:
                        recall_adapters.query_playground(self.id, body, home=self.home)
                    self.assertEqual((str(caught.exception), caught.exception.status), (code, 400))
            with self.assertRaises(TargetError) as caught:
                recall_adapters.query_playground('nope', {'prompt': PROMPT}, home=self.home)
            self.assertEqual((str(caught.exception), caught.exception.status), ('target_id_invalid', 400))
            with self.assertRaises(TargetError) as caught:
                recall_adapters.query_playground('d' * 32, {'prompt': PROMPT}, home=self.home)
            self.assertEqual(caught.exception.status, 404)
        dry_run.assert_not_called()
        longest = self.run_playground({'prompt': 'heron ' + 'x' * (recall_adapters.MAX_PROMPT_CHARS - 6),
                                       'timeoutMs': 200})
        self.assert_schema(longest)

    def test_a_refused_dry_run_keeps_the_response_shape(self):
        route = self.project.root / '.memvane' / 'route.json'
        with mock.patch('memvane.journal.api.verify_selected_route',
                        side_effect=ValueError('selected_product_unavailable')):
            view = self.run_playground({'prompt': PROMPT})
        self.assertTrue(route.exists())
        self.assert_schema(view)
        self.assertEqual((view['outcome'], view['reason']), ('refused', 'selected_product_unavailable'))
        self.assertEqual((view['fusedList'], view['selectorResult']['final']), ([], []))
