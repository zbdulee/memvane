#!/usr/bin/env python3
"""Synthetic checks for mandatory selection over published Laya cards."""
import hashlib
from contextlib import nullcontext
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_selected_host_scores_bounded_pool_and_never_uses_rrf_fallback(self):
        import laya_install_journal
        import laya_index_runtime
        import laya_local_embed_provider
        import laya_embed_resident
        import laya_incremental_docs_query
        from contextlib import ExitStack
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
        rows = [card(i) for i in range(20)]
        runtime = SimpleNamespace(index=object(), scope=SCOPE)
        with ExitStack() as stack:
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
            enter(patch.object(config, 'load_project_config', return_value={
                **decision['recallSettings'], 'contextLearning': {'liveSelection': False}}))
            with patch('context_learning.seam._private_state',
                       return_value=Path('/synthetic/state')), \
                 patch('context_learning.live_select.choose', return_value=(
                     [rows[14]['path'], rows[3]['path']], 'selected', {})) as choose:
                result = response.serve_selected('recall', 'codex', decision,
                                                 {'prompt': 'find card'})
            self.assertEqual(len(choose.call_args.args[2]), 15)
            context = result['hookSpecificOutput']['additionalContext']
            self.assertIn('wiki/card14.md', context)
            self.assertIn('wiki/card03.md', context)
            self.assertNotIn('wiki/card15.md', context)
            with patch('context_learning.seam._private_state', return_value=None):
                empty = response.serve_selected('recall', 'codex', decision,
                                                {'prompt': 'find card'})
            self.assertEqual(empty['hookSpecificOutput']['additionalContext'], '')
            with patch('context_learning.seam._private_state',
                       return_value=Path('/synthetic/state')), \
                 patch('context_learning.live_select.choose',
                       return_value=([], 'selected', {})):
                zero = response.serve_selected('recall', 'codex', decision,
                                               {'prompt': 'find card'})
            self.assertEqual(zero['hookSpecificOutput']['additionalContext'], '')
            with patch('context_learning.seam._private_state',
                       return_value=Path('/synthetic/state')), \
                 patch('context_learning.live_select.choose', return_value=(
                     [rows[14]['path']], 'selected', {})), \
                 patch.object(response, '_selection_view_unchanged', return_value=False):
                with self.assertRaisesRegex(response.HostResponseError,
                                            'selected_proof_changed'):
                    response.serve_selected('recall', 'codex', decision,
                                            {'prompt': 'find card'})


if __name__ == '__main__':
    unittest.main()
