#!/usr/bin/env python3
"""Synthetic checks for mandatory selection over published Laya cards."""
import hashlib
import json
import os
import tempfile
from contextlib import ExitStack, nullcontext
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import config
import laya_host_response as response
from context_learning import selection_port, selection_read

SCOPE = '6' * 32
PROFILE = {'profileId': 'f' * 64}


def card(index, *, generation='generation-7', scope=SCOPE, body=None):
    body = body or ('---\ntitle: Synthetic card %02d\n---\nSynthetic body %02d' %
                    (index, index))
    return {'scope': scope, 'generation': generation,
            'path': 'wiki/card%02d.md' % index,
            'hash': hashlib.sha256(body.encode()).hexdigest(),
            'body': body, 'score': 1 / (index + 1)}


class Checks(unittest.TestCase):
    def test_published_port_accepts_fifteen_without_source_reads(self):
        pool = [card(i) for i in range(15)]
        rows = selection_port.published_candidate_rows(pool, ('wiki',), SCOPE)
        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[14]['source_text'], pool[14]['body'])
        self.assertEqual(selection_port.apply_published_selection(
            pool, [pool[14]['path'], pool[0]['path']]), [pool[14], pool[0]])
        for invalid in (pool + [card(15)],
                        [card(0, scope='wrong')],
                        [{**card(0), 'body': 'changed'}],
                        [{**card(0), 'path': 'raw/card.md'}],
                        [card(0), card(0)]):
            with self.subTest(invalid=invalid[-1]['path']):
                with self.assertRaises(ValueError):
                    selection_port.published_candidate_rows(invalid, ('wiki',), SCOPE)
        with self.assertRaises(ValueError):
            selection_port.apply_published_selection(pool, ['wiki/not-returned.md'])

    def test_model_is_required_and_old_false_setting_cannot_bypass(self):
        pool = [card(i) for i in range(15)]
        cfg = config.normalize_config({'contextLearning': {'liveSelection': False}})
        self.assertNotIn('liveSelection', cfg['contextLearning'])
        with patch('context_learning.seam._private_state', return_value=Path('/synthetic/state')), \
             patch('context_learning.live_select.choose', return_value=(
                 [pool[14]['path'], pool[3]['path']], 'selected', {})) as choose:
            selected = selection_read.select_published(pool, prompt='find card',
                config=cfg, project_root='/synthetic/project',
                wiki_names=('wiki',), scope_id=SCOPE)
        self.assertEqual(selected['candidate_count'], 15)
        self.assertEqual([r['path'] for r in selected['final_results']],
                         [pool[14]['path'], pool[3]['path']])
        self.assertEqual(len(choose.call_args.args[2]), 15)
        with patch('context_learning.seam._private_state', return_value=None), \
             patch('context_learning.live_select.choose') as unavailable:
            empty = selection_read.select_published(pool, prompt='find card',
                config=cfg, project_root='/synthetic/project',
                wiki_names=('wiki',), scope_id=SCOPE)
        self.assertEqual(empty['final_results'], [])
        unavailable.assert_not_called()

    def host(self, stack, *, settings=None, rows=None):
        """Patch a selected v2 route; returns (decision, proof, rows)."""
        import laya_install_journal
        import laya_index_runtime
        import laya_local_embed_provider
        import laya_embed_resident
        import laya_incremental_docs_query
        decision = {'state': 'candidate', 'projectRoot': '/synthetic/project',
                    'pluginRoot': str(Path(response.__file__).resolve().parents[1]),
                    'dbPath': '/synthetic/index.sqlite', 'queuePath': '/synthetic/queue',
                    'dbIncarnation': 'a' * 32, 'scopeId': SCOPE,
                    'vectorProfile': PROFILE,
                    'collections': [{'name': 'raw', 'role': 'raw', 'sourceRoot': '/synthetic/project/docs'},
                                    {'name': 'wiki', 'role': 'wiki', 'sourceRoot': '/synthetic/project/wiki'}],
                    'recallSettings': {'topN': 3, 'lexicalPatterns': [], 'minScore': 0,
                                       'skipPaths': [], 'recallStrategy': 'wikiOnly'}}
        proof = {'productRouteAllowed': True, 'selectionEpoch': 1,
                 'vectorProfile': PROFILE, 'providerManifest': {}}
        rows = rows or [card(i) for i in range(20)]
        settings = settings or {**decision['recallSettings'],
                                'contextLearning': {'liveSelection': False}}
        runtime = SimpleNamespace(index=object(), scope=SCOPE)
        enter = stack.enter_context
        enter(patch.object(laya_install_journal, 'verify_selected_route', return_value=proof))
        enter(patch.object(laya_index_runtime, 'LayaIndexRuntime',
                           return_value=nullcontext(runtime)))
        enter(patch.object(laya_local_embed_provider, 'ReviewedLocalQmdProvider',
                           return_value=lambda *_args: {}))
        enter(patch.object(laya_embed_resident, 'admit_query'))
        enter(patch.object(laya_incremental_docs_query, 'query_incremental',
                           return_value=([], frozenset(), {}, (), None)))
        enter(patch.object(response, 'query_selected_v2',
                           return_value=(rows, frozenset())))
        enter(patch.object(response, '_selection_view_unchanged', return_value=True))
        enter(patch.object(config, 'load_project_config', return_value=settings))
        return decision, proof, rows, settings

    @staticmethod
    def prepared(stack):
        """A real prepared local checkpoint pointer (the #60 predicate)."""
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='laya-sel-')))
        (temp / 'active-checkpoint.json').write_text('{}')
        stack.enter_context(patch('context_learning.seam._private_state', return_value=temp))
        return temp

    def test_prepared_selector_decides_bounded_pool(self):
        with ExitStack() as stack:
            decision, proof, rows, settings = self.host(stack)
            self.prepared(stack)
            with patch('context_learning.live_select.choose', return_value=(
                    [rows[14]['path'], rows[3]['path']], 'selected', {})) as choose:
                result = response.serve_selected('recall', 'codex', decision,
                                                 {'prompt': 'find card'})
            self.assertEqual(len(choose.call_args.args[2]), 15)
            context = result['hookSpecificOutput']['additionalContext']
            self.assertIn('wiki/card14.md', context)
            self.assertIn('wiki/card03.md', context)
            self.assertNotIn('wiki/card00.md', context)
            self.assertIn('local Laya model selected', context)
            decision['recallSettings']['topN'] = settings['topN'] = 1
            with patch('context_learning.live_select.choose', return_value=(
                    [rows[14]['path'], rows[3]['path']], 'selected', {})):
                limited = response.serve_selected('recall', 'codex', decision,
                                                  {'prompt': 'find card'})
            self.assertIn('wiki/card14.md', limited['hookSpecificOutput']['additionalContext'])
            self.assertNotIn('wiki/card03.md', limited['hookSpecificOutput']['additionalContext'])
            decision['recallSettings']['topN'] = settings['topN'] = 3
            # A real zero-card choice by a prepared selector stays empty.
            with patch('context_learning.live_select.choose',
                       return_value=([], 'selected', {})):
                zero = response.serve_selected('recall', 'codex', decision,
                                               {'prompt': 'find card'})
            self.assertEqual(zero['hookSpecificOutput']['additionalContext'], '')
            with patch('context_learning.live_select.choose', return_value=(
                    [rows[14]['path']], 'selected', {})), \
                 patch.object(response, '_selection_view_unchanged', return_value=False):
                with self.assertRaisesRegex(response.HostResponseError,
                                            'selected_proof_changed'):
                    response.serve_selected('recall', 'codex', decision,
                                            {'prompt': 'find card'})
            read_only = Mock(return_value=proof)
            import laya_install_journal
            with patch('context_learning.live_select.choose', return_value=(
                    [rows[14]['path']], 'selected', {})), \
                 patch.object(laya_install_journal, 'verify_selected_route',
                              side_effect=AssertionError('wrong recheck')):
                degraded = response._serve_recall('codex', decision,
                    {'prompt': 'find card'}, proof, read_only, degraded=True)
            self.assertIn('wiki/card14.md', degraded['hookSpecificOutput']['additionalContext'])
            self.assertGreaterEqual(read_only.call_count, 2)

    def test_unprepared_selector_serves_ranked_rows(self):
        for state in (None, Path('/synthetic/state-without-checkpoint')):
            with self.subTest(state=state), ExitStack() as stack:
                decision, _proof, _rows, _settings = self.host(stack)
                log = Path(stack.enter_context(tempfile.TemporaryDirectory())) / 'recall.log'
                stack.enter_context(patch.dict(os.environ, {'QMD_RECALL_LOG': str(log)}))
                stack.enter_context(patch('context_learning.seam._private_state',
                                          return_value=state))
                choose = stack.enter_context(patch('context_learning.live_select.choose'))
                result = response.serve_selected('recall', 'codex', decision,
                                                 {'prompt': 'find card'})
                choose.assert_not_called()
                context = result['hookSpecificOutput']['additionalContext']
                for index in range(3):
                    self.assertIn('wiki/card%02d.md' % index, context)
                self.assertNotIn('wiki/card03.md', context)
                self.assertIn('hybrid rank score', context)
                self.assertNotIn('local Laya model selected', context)
                lines = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual([line['reason'] for line in lines], ['selected'])
                self.assertEqual(lines[0]['selection_fallback'], 'laya_selector_not_prepared')
                self.assertEqual(lines[0]['selected_count'], 3)

    def test_prepared_selector_failure_serves_ranked_rows(self):
        with ExitStack() as stack:
            decision, _proof, _rows, _settings = self.host(stack)
            self.prepared(stack)
            log = Path(stack.enter_context(tempfile.TemporaryDirectory())) / 'recall.log'
            stack.enter_context(patch.dict(os.environ, {'QMD_RECALL_LOG': str(log)}))
            for reason in ('timeout', 'runtime_unavailable', 'no_active_checkpoint'):
                with self.subTest(reason=reason):
                    log.write_text('')
                    with patch('context_learning.live_select.choose',
                               return_value=(None, reason, {})):
                        result = response.serve_selected('recall', 'codex', decision,
                                                         {'prompt': 'find card'})
                    context = result['hookSpecificOutput']['additionalContext']
                    self.assertIn('wiki/card00.md', context)
                    self.assertIn('hybrid rank score', context)
                    lines = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual([line['reason'] for line in lines],
                                     ['laya_selector_' + reason, 'selected'])
            with patch('context_learning.live_select.choose',
                       side_effect=OSError('selector crashed')):
                crashed = response.serve_selected('recall', 'codex', decision,
                                                  {'prompt': 'find card'})
            self.assertIn('wiki/card00.md', crashed['hookSpecificOutput']['additionalContext'])
            # The ranked rows keep the membership/view recheck.
            with patch('context_learning.live_select.choose',
                       return_value=(None, 'timeout', {})), \
                 patch.object(response, '_selection_view_unchanged', return_value=False):
                with self.assertRaisesRegex(response.HostResponseError,
                                            'selected_proof_changed'):
                    response.serve_selected('recall', 'codex', decision,
                                            {'prompt': 'find card'})

    def test_invalid_published_pool_is_never_served_ranked(self):
        rows = [card(0, scope='7' * 32)] + [card(i) for i in range(1, 5)]
        for prepared in (False, True):
            with self.subTest(prepared=prepared), ExitStack() as stack:
                decision, _proof, _rows, _settings = self.host(stack, rows=rows)
                if prepared:
                    self.prepared(stack)
                else:
                    stack.enter_context(patch('context_learning.seam._private_state',
                                              return_value=None))
                choose = stack.enter_context(patch('context_learning.live_select.choose'))
                result = response.serve_selected('recall', 'codex', decision,
                                                 {'prompt': 'find card'})
                self.assertEqual(result['hookSpecificOutput']['additionalContext'], '')
                choose.assert_not_called()

    def test_shared_trial_counts_as_prepared(self):
        self.assertTrue(selection_read.selector_prepared(
            {'sharedLearning': {'enabled': True, 'trialSelection': True}}, '/synthetic'))
        with patch('context_learning.seam._private_state', return_value=None):
            self.assertFalse(selection_read.selector_prepared({}, '/synthetic'))

    def vector_host(self, stack, *, admit=None, provider=None, owner_state=None):
        """Selected route whose query vector is missing; records the provider calls."""
        import laya_embed_resident
        import laya_local_embed_provider
        import runtime_work
        decision, proof, rows, _settings = self.host(stack)
        stack.enter_context(patch('context_learning.seam._private_state', return_value=None))
        if admit is not None:
            stack.enter_context(patch.object(laya_embed_resident, 'admit_query',
                                             side_effect=admit))
        calls = []

        def embed(*_args):
            calls.append(1)
            if provider is not None:
                raise provider
            return {}
        stack.enter_context(patch.object(laya_local_embed_provider,
                                         'ReviewedLocalQmdProvider',
                                         return_value=embed))
        stack.enter_context(patch.object(laya_embed_resident, '_failure_decision'))
        stack.enter_context(patch.object(laya_embed_resident, 'read_owner_state',
                                         return_value=owner_state))
        wake = stack.enter_context(patch.object(runtime_work, 'request',
                                                return_value={'status': 'queued'}))
        seen = []

        def lexical_only(runtime, prompt, route_profile, approved, query_provider,
                         decision_, **_kw):
            # Same contract as _pinned_vector: _VectorUnavailable -> [] vector list.
            for _ in range(2):
                try:
                    query_provider(prompt, route_profile)
                    seen.append('vector')
                except response._VectorUnavailable as exc:
                    seen.append(str(exc))
            return rows, frozenset()
        stack.enter_context(patch.object(response, 'query_selected_v2',
                                         side_effect=lexical_only))
        log = Path(stack.enter_context(tempfile.TemporaryDirectory())) / 'recall.log'
        stack.enter_context(patch.dict(os.environ, {'QMD_RECALL_LOG': str(log)}))
        return decision, wake, calls, seen, log

    def test_deferred_query_admission_serves_lexical_and_wakes_owner(self):
        from resource_deferred import ResourceDeferred
        with ExitStack() as stack:
            decision, wake, calls, seen, log = self.vector_host(
                stack, admit=ResourceDeferred('laya_query_resource_deferred'))
            result = response.serve_selected('recall', 'codex', decision,
                                             {'prompt': 'find card'})
            context = result['hookSpecificOutput']['additionalContext']
            self.assertIn('wiki/card00.md', context)
            self.assertEqual(calls, [])  # admission refused: socket never touched
            self.assertEqual(seen, ['resource_deferred', 'resource_deferred'])
            wake.assert_called_once_with('laya-sync', decision['projectRoot'])
            lines = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([line['reason'] for line in lines],
                             ['laya_vector_selected_provider_resource_deferred', 'selected'])
            self.assertEqual(lines[1]['vector_fallback'],
                             'selected_provider_resource_deferred')

    def test_cold_provider_serves_lexical_and_wakes_owner(self):
        from laya_index_a import IndexContractError
        for state, code in ((None, 'selected_provider_preparing'),
                            ({'terminal': False, 'reason': 'resource_deferred'},
                             'selected_provider_resource_deferred'),
                            ({'terminal': False, 'reason': 'worker_exited'},
                             'selected_provider_retry_wait')):
            with self.subTest(code=code), ExitStack() as stack:
                decision, wake, calls, seen, log = self.vector_host(
                    stack, provider=IndexContractError('provider_unavailable'),
                    owner_state=state)
                result = response.serve_selected('recall', 'codex', decision,
                                                 {'prompt': 'find card'})
                self.assertIn('wiki/card00.md',
                              result['hookSpecificOutput']['additionalContext'])
                self.assertEqual(calls, [1])  # one socket attempt per prompt
                self.assertEqual(seen, ['provider_unavailable'] * 2)
                wake.assert_called_once_with('laya-sync', decision['projectRoot'])
                reasons = [json.loads(line)['reason']
                           for line in log.read_text().splitlines()]
                self.assertEqual(reasons, ['laya_vector_' + code, 'selected'])
        # A terminal provider failure still refuses (it needs repair).
        with ExitStack() as stack:
            decision, wake, _calls, _seen, _log = self.vector_host(
                stack, provider=IndexContractError('provider_unavailable'),
                owner_state={'terminal': True, 'reason': 'attestation_invalid'})
            with self.assertRaisesRegex(response.HostResponseError,
                                        '^selected_provider_start_failed$'):
                response.serve_selected('recall', 'codex', decision,
                                        {'prompt': 'find card'})
            wake.assert_not_called()

if __name__ == '__main__':
    unittest.main()
