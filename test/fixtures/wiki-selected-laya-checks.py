"""Synthetic selected wiki boundary; no extractor, model or live journal."""
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import sys
import hashlib
import json
import os
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
            'recallSettings': {'skipPaths': []},
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

    def test_local_retrieval_withholds_mixed_source_excluded_card(self):
        def revision(path):
            body = path.encode()
            return {'kind': 'file', 'path': path, 'collection': 'raw',
                    'sha256': hashlib.sha256(body).hexdigest(),
                    'size': len(body), 'mtimeNs': 1}
        card = ('---\nsourceRevisions:\n  - ' +
                json.dumps(revision('docs/keep/a.md')) + '\n  - ' +
                json.dumps(revision('docs/build/b.md')) +
                '\n---\n# Card\nstarship archive\n')
        (self.wiki / 'mixed.md').write_text(card, encoding='utf-8')
        self.decision['recallSettings']['skipPaths'] = ['build']
        with patch.object(selected, 'recheck'):
            rows = selected.wiki_matches(self.root, self.wiki,
                                         'starship archive', 3, 100,
                                         bound=(self.decision, self.proof))
        self.assertEqual(rows, [])

    def test_old_verify_job_from_excluded_source_never_loads_model(self):
        import wiki_verify_worker as verifier
        self.decision['recallSettings']['skipPaths'] = ['build']
        job = {'targetPath': '.auto-context/wiki/mixed.md',
               'sourceRevisions': [{'path': 'docs/build/a.md'}]}
        with patch.object(selected, 'selected', return_value=(self.decision, self.proof)), \
             patch.object(verifier, 'card_state', return_value=(
                 '---\n---\n# Card\n', {'createdBy': 'qmd-auto-context'},
                 'generated', '')), \
             patch.object(verifier, 'snapshot_revision_evidence') as snapshot, \
             patch.object(verifier, 'log_verdict') as log:
            result = verifier.process_verify_job(
                self.root, {}, {}, {}, job, self.root / 'verify.log')
        self.assertEqual(result, (True, False))
        self.assertEqual(log.call_args.args[1]['reason'], 'source_folder_excluded')
        snapshot.assert_not_called()

    def test_verify_rejects_excluded_card_revision_with_crlf(self):
        import wiki_verify_worker as verifier
        self.decision['recallSettings']['skipPaths'] = ['build']
        def revision(path):
            return {'kind': 'file', 'path': path, 'collection': 'raw',
                    'sha256': hashlib.sha256(path.encode()).hexdigest(),
                    'size': len(path), 'mtimeNs': 1}
        included = revision('docs/keep/a.md')
        excluded = revision('docs/build/b.md')
        card = ('---\r\nsourceRevisions:\r\n  - ' + json.dumps(included)
                + '\r\n  - ' + json.dumps(excluded) + '\r\n---\r\n# Card\r\n')
        job = {'targetPath': '.auto-context/wiki/mixed.md',
               'sourceRevisions': [included],
               'sources': [{'kind': 'file', 'path': included['path']}]}
        with patch.object(selected, 'selected', return_value=(self.decision, self.proof)), \
             patch.object(verifier, 'card_state', return_value=(
                 card, {'createdBy': 'qmd-auto-context'}, 'generated', '')), \
             patch.object(verifier, 'snapshot_revision_evidence') as snapshot, \
             patch.object(verifier, 'log_verdict') as log:
            result = verifier.process_verify_job(
                self.root, {}, {}, {}, job, self.root / 'verify.log')
        self.assertEqual(result, (True, False))
        self.assertEqual(log.call_args.args[1]['reason'], 'source_folder_excluded')
        snapshot.assert_not_called()

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
             patch.object(selected, 'request_sync') as sync:
            wiki_verify_worker.reindex_wiki(self.root, {})
            sync.assert_called_once()

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


    def _orphan_claim(self, queue_name, pid):
        compile_dir = self.root / '.auto-context' / 'compile'
        compile_dir.mkdir(parents=True, exist_ok=True)
        claim = compile_dir / (queue_name + '.claimed.%d.%s' % (pid, 'f' * 32))
        claim.write_text('{"source":"orphan"}\n', encoding='utf-8')
        return compile_dir / queue_name, claim

    def test_orphan_claim_with_empty_queue_is_not_drained(self):
        # The worker died after claiming: its rows live only in the claim
        # file while source-queue.jsonl is absent. That is not an idle queue.
        queue, claim = self._orphan_claim('source-queue.jsonl', 4242421)
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': '4242421'}), \
             patch.object(selected, 'selected', return_value=(self.decision, self.proof)), \
             patch.object(selected, 'recheck'), \
             patch('wiki_lifecycle_request.claim', return_value={'claimed': True}), \
             patch('wiki_lifecycle_request.run_lifecycle'), \
             patch('wiki_freshness.unresolved_pending_refreshes_strict', return_value=[]), \
             patch.object(selected.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout='{"processed":0,"remaining":1}')) as child:
            result = selected.run(self.root)
        self.assertEqual(result['state'], 'pending')
        child.assert_called_once()
        self.assertFalse(claim.exists())
        self.assertIn('"orphan"', queue.read_text(encoding='utf-8'))

    def test_live_owner_claim_keeps_the_wake_pending(self):
        # A claim of a live owner other than this process is work in flight.
        _queue, claim = self._orphan_claim('verify-queue.jsonl', os.getppid())
        with patch('wiki_freshness.unresolved_pending_refreshes_strict', return_value=[]):
            idle = selected._compile_idle(
                self.root, self.root / '.auto-context' / 'compile' / 'source-queue.jsonl',
                self.root / '.auto-context' / 'compile' / 'verify-queue.jsonl',
                (self.decision, self.proof))
        self.assertIs(idle, False)
        self.assertTrue(claim.exists())


if __name__ == '__main__':
    unittest.main()
