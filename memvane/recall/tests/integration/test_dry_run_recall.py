"""The selected recall's dry run (``recall.api.dry_run_recall``).

It reports the four stages of the prompt recall's read (FTS lists with bm25,
semantic cosines, RRF scores, the selector's choice) for the candidate,
linked-worktree and preview routes, and it changes nothing: no event line,
stderr line, sync or provider wake, inference service or trainer start,
learning case or edit-recall session, and no read of the hook budget.
"""
from __future__ import annotations

from contextlib import closing, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import time
from unittest import mock

from memvane.recall import api as recall_api
from memvane.recall.tests.support import NOTE, RecallInstallCase
from memvane.recall.types import DryRunRecallResult
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

PROMPT = 'what marker does the heron protocol keep?'


def _no_hook_budget():
    """Patches that fail the test when anything reads the process hook budget."""
    def refused(*_args, **_kwargs):
        raise AssertionError('the dry run read the hook budget')
    return [mock.patch('memvane.kernel.hook_budget.remaining', side_effect=refused),
            mock.patch('memvane.kernel.hook_budget.deadline', side_effect=refused)]


def _tree(*roots):
    """Every file under ``roots`` with its size and mtime (sqlite side files excluded)."""
    found = {}
    for root in roots:
        for path in Path(root).rglob('*'):
            if path.is_file() and not path.name.endswith(('-wal', '-shm', '-journal')):
                info = path.stat()
                found[str(path)] = (info.st_size, info.st_mtime_ns)
    return found


class DryRunCase(RecallInstallCase):
    CARDS = 2

    def dry_run(self, prompt=PROMPT, *, cwd=None, seconds=20, **options):
        """One dry run; asserts it wrote no event, stderr line or wake."""
        before = len(self.install.events())
        err = io.StringIO()
        patches = _no_hook_budget()
        for patch in patches:
            patch.start()
        try:
            with redirect_stderr(err):
                result = recall_api.dry_run_recall(
                    str(cwd or self.project), prompt, deadline=time.monotonic() + seconds, **options)
        finally:
            for patch in patches:
                patch.stop()
        self.assertIsInstance(result, DryRunRecallResult)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(len(self.install.events()), before)
        self.assertEqual(self.wakes, [])
        return result

    def served_paths(self, prompt=PROMPT):
        """The fused paths the prompt recall's own read returns for ``prompt``."""
        results = []
        real = recall_api.query_endpoint

        def observed(*args, **kwargs):
            results.append(real(*args, **kwargs))
            return results[-1]
        with mock.patch('memvane.recall.api.query_endpoint', side_effect=observed):
            self.recall(prompt)
        self.wakes.clear()
        return [row['path'] for row in results[-1].rows], results[-1].semantic_status

    def forge_reader_without_vector_spec(self):
        """The selected reader record as a lexical-first seal leaves it: no vector spec."""
        from memvane.journal.api import inspect_journal
        record = dict(inspect_journal(self.install.journal)['reader'],
                      vector_profile=None, vector_recipe=None)
        raw = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False)
        with closing(sqlite3.connect(str(self.install.journal), isolation_level=None)) as db:
            db.execute('UPDATE reader SET record=? WHERE id=1', (raw,))

    def publish_changed_lexical_head(self):
        self.fixture.write_card('concepts/glacier.md', title='Glacier protocol', sources=[self.note],
                                body='The current edition keeps zqneweditionx.\n')
        with mock.patch('memvane.store.api.provider_spec', return_value=None):
            result = self.install.run_sync()
        self.assertEqual(result['vector']['state'], 'off', result)
        return result['generation']


class DryRunStages(DryRunCase):
    def test_four_stages_match_the_prompt_recall_read_and_change_nothing(self):
        self.install.provider.admit_queries()
        served, status = self.served_paths()
        self.assertEqual(status, 'complete')
        sessions = self.install.state / 'edit-recall-sessions'
        held = sorted(sessions.rglob('*')) if sessions.exists() else []
        tree = _tree(self.home, self.install.state)
        result = self.dry_run()
        self.assertEqual(_tree(self.home, self.install.state), tree)
        self.assertEqual(sorted(sessions.rglob('*')) if sessions.exists() else [], held)
        view = result.as_dict()
        self.assertEqual((view['route'], view['linked'], view['outcome'], view['reason']),
                         ('candidate', False, 'served', 'ranked'))
        self.assertEqual(view['semanticStatus'], 'complete')
        self.assertIsNone(view['fallbackReason'])
        self.assertTrue(view['lexicalLists'])
        for lane in view['lexicalLists']:
            self.assertIsInstance(lane['query'], str)
            self.assertEqual([row['rank'] for row in lane['rows']], list(range(1, len(lane['rows']) + 1)))
            self.assertTrue(all(isinstance(row['score'], float) for row in lane['rows']))
        self.assertTrue(view['semanticList'])
        self.assertTrue(all(-1.0 <= row['cosine'] <= 1.0 for row in view['semanticList']))
        fused = view['fusedList']
        self.assertEqual([row['path'] for row in fused], served)
        self.assertEqual([row['rrfScore'] for row in fused],
                         sorted((row['rrfScore'] for row in fused), reverse=True))
        self.assertTrue(all(row['rrfScore'] > 1 / 61 for row in fused[:1]))
        selector = view['selectorResult']
        self.assertEqual((selector['ran'], selector['reason']), (False, 'selector_disabled'))
        self.assertEqual([row['path'] for row in selector['final']], served[:min(3, len(served))])
        self.assertEqual({row['decision'] for row in selector['decisions'][:len(selector['final'])]},
                         {'ranked_top'})
        self.assertNotIn('body', json.dumps(view))
        for key in ('lexicalMs', 'semanticMs', 'queryEmbeddingMs', 'selectorMs', 'totalMs'):
            self.assertGreaterEqual(view['timings'][key], 0)
        self.assertIn(view['timings']['arenaHit'], (True, False))
        with self.assertRaises(AttributeError):
            result.outcome = 'empty'
        self.assertEqual(list(self.home.rglob('learning.sqlite3')), [])

    def test_semantic_off_asks_no_provider(self):
        self.install.provider.admit_queries()
        self.install.provider.bound_queries()
        result = self.dry_run(use_semantic=False)
        self.assertEqual((result.outcome, result.semantic_status, result.fallback_reason),
                         ('served', 'provider_absent', 'semantic_disabled'))
        self.assertIsNone(result.semantic_list)
        self.assertEqual(self.install.provider.query_lengths(), [])

    def test_lexical_first_view_reports_partial_coverage(self):
        generation = self.publish_changed_lexical_head()
        self.forge_reader_without_vector_spec()
        self.install.provider.admit_queries()
        result = self.dry_run('zqnoftsmatchx')
        self.assertEqual((result.generation, result.semantic_status), (generation, 'partial'))
        self.assertEqual((result.vector_covered_members, result.vector_eligible_members), (1, 2))
        self.assertIsNone(result.fallback_reason)
        self.assertEqual([row['path'].rsplit('/', 1)[-1] for row in result.semantic_list], ['heron.md'])
        self.assertEqual([(row['lexical'], row['semantic']) for row in result.fused_list], [(False, True)])

    def test_terminal_provider_serves_lexical_and_identity_change_refuses(self):
        from memvane.kernel.errors import IndexContractError
        from memvane.provider import api as provider
        from memvane.journal.api import select_route, verify_selected_route
        self.install.provider.admit_queries()
        self.install.provider.answer_queries('provider_unavailable')
        decision = select_route(str(self.project), action='recall')
        manifest = verify_selected_route(decision)['providerManifest']
        state = Path(manifest['serviceSocketPath'] + '.resident.state')
        with os.fdopen(os.open(state, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as handle:
            json.dump(dict(fingerprint=manifest['embeddingFingerprint'], reason='attestation_invalid',
                           terminal=True, at=time.time(), attempts=3), handle)
        raw = state.read_bytes()
        with mock.patch('memvane.resident.api.ensure_running',
                        side_effect=AssertionError('provider start')):
            result = self.dry_run()
        self.assertEqual((result.outcome, result.semantic_status, result.fallback_reason),
                         ('served', 'provider_absent', 'selected_provider_start_failed'))
        self.assertIn('laya_vector_selected_provider_start_failed', result.notes)
        self.assertEqual(state.read_bytes(), raw)
        with mock.patch.object(provider, 'query_vectors',
                               side_effect=IndexContractError('provider_identity_mismatch')):
            refused = self.dry_run()
        self.assertEqual((refused.outcome, refused.reason),
                         ('refused', 'selected_provider_identity_transition'))
        self.assertEqual(refused.fused_list, ())

    def test_cold_provider_reports_preparing_without_a_wake(self):
        self.install.provider.admit_queries()
        self.install.provider.answer_queries('provider_unavailable')
        result = self.dry_run()
        self.assertEqual((result.outcome, result.fallback_reason),
                         ('served', 'selected_provider_preparing'))
        self.assertEqual(result.notes, ('laya_vector_selected_provider_preparing',))

    def test_invalid_input_refuses_with_a_fixed_code(self):
        self.assertEqual(self.dry_run('  ').reason, 'invalid_query_prompt')
        result = recall_api.dry_run_recall(str(self.project), PROMPT, deadline=time.monotonic() - 1)
        self.assertEqual((result.outcome, result.reason), ('refused', 'invalid_deadline'))
        self.assertEqual(self.dry_run(cwd='relative/path').reason, 'invalid_target_root')

    def test_a_checkout_outside_any_selected_route_is_not_selected(self):
        outside = self.tmp / 'plain'
        outside.mkdir()
        result = self.dry_run(cwd=outside)
        self.assertEqual((result.outcome, result.reason), ('empty', 'route_not_selected'))
        self.assertNotIn(result.route, ('candidate', 'worktree', 'preview'))


class DryRunSelector(DryRunCase):
    def test_a_cold_selector_is_never_asked_or_started(self):
        refused = mock.Mock(side_effect=AssertionError('selector asked'))
        start = mock.Mock(side_effect=AssertionError('service start'))
        roots = []
        from memvane.runtime import api as runtime_api
        real_root = runtime_api.service_root

        def service_root(**kwargs):
            roots.append(kwargs.get('create', False))
            return real_root(**kwargs)
        with mock.patch('memvane.learning.api.select_published', refused), \
                mock.patch('memvane.resident.api.ensure_service', start), \
                mock.patch('memvane.runtime.api.service_root', side_effect=service_root):
            with mock.patch('memvane.learning.api.selector_prepared', return_value=False):
                result = self.dry_run(use_selector=True)
            self.assertEqual((result.outcome, result.selector_result['reason'], result.reason),
                             ('served', 'selector_not_prepared', 'ranked'))
            self.assertIn('laya_selector_selector_not_prepared', result.notes)
            with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                    mock.patch('memvane.learning.api.inference_version', return_value=None):
                result = self.dry_run(use_selector=True)
            self.assertEqual(result.selector_result['reason'], 'selector_not_warm')
            self.assertFalse(result.selector_result['ran'])
            self.assertTrue(result.selector_result['final'])
        refused.assert_not_called()
        start.assert_not_called()
        self.assertNotIn(True, roots)

    def test_a_warm_selector_answers_with_a_start_refusing_callback(self):
        asked = []

        def select(pool, **kwargs):
            asked.append(kwargs)
            with self.assertRaises(PermissionError):
                kwargs['ensure_service'](python='/x', learning_root=self.tmp)
            return {'reason': 'selected', 'final_results': pool[:1],
                    'resources': {'checkpoint': 'abcdef012345', 'pairs': 4}}
        warm = self.tmp / 'warm-service'
        warm.mkdir(mode=0o700)
        with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                mock.patch('memvane.runtime.api.service_root', return_value=warm), \
                mock.patch('memvane.learning.api.inference_version', return_value={'status': 'ready'}), \
                mock.patch('memvane.learning.api.select_published', side_effect=select):
            result = self.dry_run(use_selector=True)
        self.assertEqual(len(asked), 1)
        self.assertTrue(asked[0]['skip_busy'])
        view = result.as_dict()
        self.assertEqual((view['outcome'], view['reason']), ('served', 'selected'))
        selector = view['selectorResult']
        self.assertEqual((selector['ran'], selector['reason'], selector['checkpoint']),
                         (True, 'selected', 'abcdef012345'))
        self.assertEqual(len(selector['final']), 1)
        decisions = [row['decision'] for row in selector['decisions']]
        self.assertEqual(decisions[0], 'selected')
        self.assertTrue(set(decisions[1:]) <= {'not_selected', 'outside_selector_candidates'})
        self.assertEqual(view['timings']['selectorPairs'], 4)


class DryRunWorktree(DryRunCase):
    CARDS = 1

    def test_a_linked_worktree_reads_its_own_view_once_published(self):
        linked = self.fixture.linked_worktree('wt', 'wt-branch')
        preparing = self.dry_run(cwd=linked.root)
        self.assertEqual((preparing.route, preparing.linked, preparing.outcome, preparing.reason),
                         ('worktree', True, 'empty', 'view_preparing'))
        self.install.settle()
        result = self.dry_run(cwd=linked.root)
        self.assertEqual((result.route, result.linked, result.outcome), ('worktree', True, 'served'))
        self.assertTrue(any(row['path'].endswith('heron.md') for row in result.fused_list))


class DryRunPreview(FixtureCase):
    def test_a_preview_route_answers_empty(self):
        fixture = make_project(self.home)
        note = fixture.write_note('notes/heron.md', NOTE)
        fixture.write_card('heron.md', title='Emerald heron', body=NOTE, sources=[note])
        fixture.commit('fixture')
        install = build_install(self, fixture, upto='prepared')
        before = len(install.events())
        result = recall_api.dry_run_recall(str(fixture.root), PROMPT, deadline=time.monotonic() + 10)
        self.assertEqual((result.route, result.outcome, result.reason),
                         ('preview', 'empty', 'initial_build_preview'))
        self.assertEqual(len(install.events()), before)
