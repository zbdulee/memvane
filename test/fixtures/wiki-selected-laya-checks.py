"""Synthetic selected wiki boundary; no extractor, model or live journal."""
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import wiki_selected_laya as selected


class SelectedWikiChecks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='wiki-selected-laya-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.wiki = self.root / '.auto-context' / 'wiki'
        self.wiki.mkdir(parents=True)
        self.decision = {
            'state': 'candidate', 'projectRoot': str(self.root),
            'installId': 'a' * 32, 'generation': 'g-' + 'b' * 32,
            'scopeId': 'c' * 32, 'dbIncarnation': 'd' * 32,
            'collections': [
                {'name': 'raw', 'role': 'raw', 'sourceRoot': str(self.root)},
                {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)},
            ],
        }
        self.proof = {'productRouteAllowed': True}

    def test_selected_route_requires_installer_proof(self):
        with patch('laya_index_host_route.select_route', return_value=self.decision), \
             patch('laya_install_journal.verify_selected_route', return_value=self.proof):
            bound = selected.selected(self.root)
            self.assertEqual(bound, (self.decision, self.proof))
            self.assertEqual(selected.route_identity(bound), {
                'installId': 'a' * 32, 'generation': 'g-' + 'b' * 32,
                'scopeId': 'c' * 32, 'dbIncarnation': 'd' * 32})
        with patch('laya_index_host_route.select_route', return_value={
            'state': 'blocked', 'reason': 'route_changed'}):
            with self.assertRaisesRegex(ValueError, 'route_unavailable'):
                selected.selected(self.root)

    def test_local_retrieval_sees_unpublished_card_without_qmd(self):
        (self.wiki / 'one.md').write_text('# Mirror\nA starship archive', encoding='utf-8')
        (self.wiki / 'two.md').write_text('# Other\nA garden', encoding='utf-8')
        bound = (self.decision, self.proof)
        with patch.object(selected, 'recheck'):
            rows = selected.wiki_matches(self.root, self.wiki,
                                         'starship archive', 3, 100, bound=bound)
        self.assertEqual([row[0].name for row in rows], ['one.md'])
        self.assertGreater(rows[0][1], 0)

    def test_route_change_prevents_selected_wake(self):
        bound = (self.decision, self.proof)
        with patch('laya_install_journal.verify_selected_route',
                   return_value={'productRouteAllowed': False}), \
             patch('runtime_work.request') as request:
            with self.assertRaisesRegex(ValueError, 'proof_changed'):
                selected.request_compile(self.root, bound)
            request.assert_not_called()

    def test_verify_reindex_queues_laya_only(self):
        import wiki_verify_worker
        with patch('wiki_compile.find_wiki_collection', return_value=('wiki', '.auto-context/wiki')), \
             patch.object(selected, 'selected', return_value=(self.decision, self.proof)), \
             patch.object(selected, 'request_sync') as sync, \
             patch.object(wiki_verify_worker, 'enqueue_project_collections') as qmd:
            wiki_verify_worker.reindex_wiki(self.root, {})
            sync.assert_called_once()
            qmd.assert_not_called()

    def test_durable_worker_does_not_ack_pending_source(self):
        queue = self.root / '.auto-context' / 'compile' / 'source-queue.jsonl'
        queue.parent.mkdir(parents=True)
        queue.write_text('{"source":"pending"}\n', encoding='utf-8')
        with patch.object(selected, 'selected', return_value=(self.decision, self.proof)), \
             patch.object(selected, 'recheck'), \
             patch.object(selected.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout='{"processed":0,"remaining":1}')) as child:
            result = selected.run(self.root)
        self.assertEqual(result['state'], 'pending')
        self.assertIn('wiki_compile_worker.py', child.call_args.args[0][2])


if __name__ == '__main__':
    unittest.main()
