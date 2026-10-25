"""Synthetic selected-Laya paid-extraction idempotence; no model, journal or QMD.

The fake extractor only counts calls. Cards are written in the compiler's
frontmatter shape and promoted by the real single writer
(`wiki_compile.stamp_verification`), so the skip is judged by the real selected
card parsing/trust contract, not by a test-made shortcut.
"""
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import os
import sys
import tempfile
import tracemalloc
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import wiki_compile as wc
import wiki_compile_worker as worker
import wiki_freshness
import wiki_selected_laya
import yaml_scalars as ys


class RevisionSkipChecks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='wiki-revision-skip-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.docs = self.root / 'docs'
        self.wiki = self.root / '.auto-context' / 'wiki'
        self.docs.mkdir(parents=True)
        self.wiki.mkdir(parents=True)
        (self.root / '.auto-context' / 'compile').mkdir(parents=True)
        self.source = self.docs / 'source.md'
        self.source.write_text('# Source\n\nDurable decision: cards cite source.\n', encoding='utf-8')
        self.config = {
            'indexing': True,
            'collections': ['proj-docs', 'proj-wiki'],
            'collectionPaths': {'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki'},
            'collectionRoles': {'proj-docs': 'raw', 'proj-wiki': 'wiki'},
            'wikiPath': '.auto-context/wiki',
            'compile': {
                'mode': 'guarded', 'defaultStatus': 'generated',
                'triggers': ['post_tool_source', 'manual'], 'maxSourceChars': 12000,
                'extractor': {'backends': {'claude': ['fake-extractor']}, 'timeout': 30},
            },
        }
        (self.root / '.auto-context' / 'settings.json').write_text(
            json.dumps(self.config), encoding='utf-8')
        self.decision = {
            'state': 'candidate', 'projectRoot': str(self.root),
            'installId': 'a' * 32, 'generation': 'g-' + 'b' * 32,
            'scopeId': 'c' * 32, 'dbIncarnation': 'd' * 32,
            'collections': [
                {'name': 'proj-docs', 'role': 'raw', 'sourceRoot': str(self.docs)},
                {'name': 'proj-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)},
            ],
            'collectionRoots': {'proj-docs': str(self.docs), 'proj-wiki': str(self.wiki)},
        }
        self.bound = (self.decision, {'productRouteAllowed': True})
        self.extractor_calls = []
        self.cards = []
        self.revisions = []
        patches = [
            patch.dict(os.environ, {'QMD_DIRTY_QUEUE': str(self.root / 'dirty-queue')}),
            patch.object(wiki_selected_laya, 'selected', side_effect=lambda _root: self.bound),
            patch.object(wiki_selected_laya, 'recheck'),
            patch.object(worker, 'run_extractor', side_effect=self.fake_extractor),
            patch.object(worker, 'compile_candidate', side_effect=self.fake_compile),
            patch.object(worker, 'gather_similar_pages', return_value=[]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def fake_extractor(self, argv, payload, timeout, root):
        self.extractor_calls.append(payload['trigger'])
        return {'candidates': [{'title': 'Source decision', 'summary': 'Cards cite source.'}]}, None, 0

    def fake_compile(self, root, candidate):
        card = self.wiki / 'concepts' / f'card-{len(self.cards) + 1}.md'
        card.parent.mkdir(parents=True, exist_ok=True)
        revisions = ys.dump_source_revisions(candidate['sourceRevisions'])
        card.write_text('\n'.join([
            '---', 'title: "Source decision"', 'status: generated',
            'createdBy: qmd-auto-context', 'sources:',
            '  - {kind: "file", path: "docs/source.md", collection: "proj-docs"}',
            'sourceRevisions:', *(f'  - {item}' for item in revisions), '---', '',
            'Generated wiki pages cite source markdown.', '',
        ]), encoding='utf-8')
        self.cards.append(card)
        self.revisions.append(candidate['sourceRevisions'])
        return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions'],
                'targetPath': str(card)}

    def verify(self, card):
        body_hash = ys.card_body_hash(card.read_text(encoding='utf-8'))
        self.assertTrue(wc.stamp_verification(card, 'verified', 'codex', 'cross-engine', body_hash))

    def job(self, trigger):
        return {
            'ts': '2026-10-19T00:00:00Z', 'trigger': trigger, 'engine': 'claude',
            'cwd': str(self.root),
            'source': {'kind': 'file', 'path': 'docs/source.md', 'collection': 'proj-docs'},
            'selectedRoute': wiki_selected_laya.route_identity(self.bound),
        }

    def process(self, trigger):
        return worker.process_job(self.root, self.config, self.config['compile'], self.job(trigger))

    def skip_reasons(self):
        path = worker.candidate_path(self.root, self.config['compile'])
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
        return [row for row in rows if row.get('reason') == 'already_compiled_revision']

    def compile_verified_once(self):
        self.assertEqual(self.process('post_tool_source'), (True, False, []))
        self.assertEqual(len(self.extractor_calls), 1)
        self.verify(self.cards[0])

    def test_same_revision_across_triggers_and_processed_queue_extracts_once(self):
        self.compile_verified_once()
        for trigger in ('session_start', 'file_change', 'post_tool_source', 'manual'):
            self.assertEqual(self.process(trigger), (True, False, []), trigger)
        self.assertEqual(self.extractor_calls, ['post_tool_source'])
        skipped = self.skip_reasons()
        self.assertEqual(len(skipped), 4)
        self.assertEqual({row['card'] for row in skipped}, {'concepts/card-1.md'})
        self.assertNotIn('content', json.dumps(skipped))

        # Separate claims of the real queue: each run claims, processes and ACKs.
        queue = self.root / '.auto-context' / 'compile' / 'source-queue.jsonl'
        with patch('setup_guard.status', return_value={'status': 'ready'}), \
             patch.object(worker, '_run_verify_pass'):
            for trigger in ('session_start', 'manual'):
                queue.write_text(json.dumps(self.job(trigger)) + '\n', encoding='utf-8')
                out = io.StringIO()
                with patch.object(sys, 'argv', ['wiki_compile_worker.py', '--cwd', str(self.root),
                                                '--json', '--flush-all']), redirect_stdout(out):
                    self.assertEqual(worker.main(), 0)
                self.assertEqual(json.loads(out.getvalue())['processed'], 1)
                self.assertEqual(queue.read_text(encoding='utf-8'), '')
        self.assertEqual(len(self.extractor_calls), 1)

        # Changed bytes are a new revision: one more paid extraction, then idempotent again.
        self.source.write_text('# Source\n\nDurable decision: cards cite the source.\n', encoding='utf-8')
        self.process('file_change')
        self.assertEqual(len(self.extractor_calls), 2)
        self.verify(self.cards[1])
        self.process('session_start')
        self.assertEqual(len(self.extractor_calls), 2)

    def test_partial_failure_is_retried_even_after_a_sibling_is_verified(self):
        calls = []

        def two_candidates(argv, payload, timeout, root):
            self.extractor_calls.append(payload['trigger'])
            return {'candidates': [{'title': 'A', 'summary': 'a'},
                                   {'title': 'B', 'summary': 'b'}]}, None, 0

        def a_then_reject(root, candidate):
            calls.append(candidate.get('title'))
            if candidate.get('title') == 'B' and len(calls) <= 2:
                return {'action': 'rejected'}
            return self.fake_compile(root, candidate)
        with patch.object(worker, 'run_extractor', side_effect=two_candidates), \
                patch.object(worker, 'compile_candidate', side_effect=a_then_reject):
            job = self.job('file_change')
            processed, preserve, _queued = worker.process_job(
                self.root, self.config, self.config['compile'], job)
            self.assertEqual((processed, preserve), (False, True))
            self.assertIs(job.get('partialCompile'), True)
            self.verify(self.cards[0])  # A becomes trusted before the retry
            worker.process_job(self.root, self.config, self.config['compile'], job)
        self.assertEqual(len(self.extractor_calls), 2, 'the retry extracts again')
        self.assertEqual(self.skip_reasons(), [])
        self.assertIn('B', calls[2:])

    def test_unverified_card_does_not_skip(self):
        self.process('post_tool_source')
        self.process('session_start')  # card is still `generated`
        self.assertEqual(len(self.extractor_calls), 2)
        self.assertEqual(self.skip_reasons(), [])

    def test_untrusted_sibling_for_same_revision_blocks_skip(self):
        self.compile_verified_once()
        # A still-`generated` card naming the same revision makes the evidence ambiguous.
        self.fake_compile(self.root, {'sourceRevisions': self.revisions[0]})
        self.process('manual')
        self.assertEqual(len(self.extractor_calls), 2)
        self.assertEqual(self.skip_reasons(), [])

    def test_stale_or_tampered_card_does_not_skip(self):
        self.compile_verified_once()
        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.process('session_start')  # same bytes, new mtime: selected recall would not serve it
        self.assertEqual(len(self.extractor_calls), 2)
        self.verify(self.cards[1])
        card = self.cards[1]
        card.write_text(card.read_text(encoding='utf-8') + 'tampered\n', encoding='utf-8')
        self.process('manual')  # verifiedBodyHash no longer matches
        self.assertEqual(len(self.extractor_calls), 3)

    def test_pending_refresh_or_unknown_ledger_does_not_skip(self):
        self.compile_verified_once()
        self.assertIsNotNone(wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude'))
        self.process('post_tool_source')
        self.assertEqual(len(self.extractor_calls), 2)
        self.assertEqual(self.skip_reasons(), [])

    def test_corrupt_pending_ledger_does_not_skip(self):
        self.compile_verified_once()
        ledger = wiki_freshness._pending_path(self.root)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"torn', encoding='utf-8')
        self.process('session_start')
        self.assertEqual(len(self.extractor_calls), 2)

    def test_symlinked_wiki_card_does_not_skip(self):
        self.compile_verified_once()
        outside = self.root / 'outside.md'
        outside.write_text('# outside\n', encoding='utf-8')
        (self.wiki / 'linked.md').symlink_to(outside)
        self.process('session_start')
        self.assertEqual(len(self.extractor_calls), 2)

    def sparse_card(self, rel, size):
        path = self.wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'wb') as out:
            out.truncate(size)  # sparse: cheap on disk, full-size to any reader
        return path

    def proof_peak(self, revision):
        tracemalloc.start()
        try:
            card = worker.selected_compiled_revision_card(self.root, self.bound, revision)
            return card, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_oversized_card_fails_closed_without_unbounded_read(self):
        self.compile_verified_once()
        revision = self.revisions[0][0]
        self.assertEqual(worker.selected_compiled_revision_card(self.root, self.bound, revision),
                         'concepts/card-1.md')
        budget = worker.SELECTED_PROOF_MAX_BYTES
        remaining = budget - self.cards[0].stat().st_size
        # Exactly the remaining aggregate budget still proves the revision.
        filler = self.sparse_card('notes/filler.md', remaining)
        self.assertEqual(worker.selected_compiled_revision_card(self.root, self.bound, revision),
                         'concepts/card-1.md')
        # One byte past the remaining aggregate (each card under the per-file cap).
        with open(filler, 'r+b') as out:
            out.truncate(remaining + 1)
        card, peak = self.proof_peak(revision)
        self.assertIsNone(card)
        self.assertLess(peak, budget // 5)  # rejected before allocating its bytes
        # One card alone past the per-file cap.
        filler.unlink()
        self.sparse_card('notes/huge.md', budget + 1)
        card, peak = self.proof_peak(revision)
        self.assertIsNone(card)
        self.assertLess(peak, budget // 5)
        # The real worker keeps the paid path instead of skipping.
        self.process('session_start')
        self.assertEqual(len(self.extractor_calls), 2)
        self.assertEqual(self.skip_reasons(), [])

    def test_card_growing_during_read_fails_closed(self):
        card = self.wiki / 'grow.md'
        card.write_bytes(b'x' * 1000)
        real_read = os.read
        requested = []

        def growing_read(fd, n):
            requested.append(n)
            if len(requested) == 1:
                with open(card, 'ab') as out:
                    out.write(b'y' * 4096)
            return real_read(fd, n)

        with patch.object(worker.os, 'read', side_effect=growing_read):
            self.assertIsNone(worker._read_proof_card_bounded(card, 1000))
        self.assertLessEqual(sum(requested), 1001)  # never more than one byte past the limit
        self.assertIsNone(worker._read_proof_card_bounded(card, 1000))  # now oversized by fstat
        self.assertEqual(worker._read_proof_card_bounded(card, card.stat().st_size)[:1], b'x')

    def run_main(self):
        out = io.StringIO()
        with patch.object(sys, 'argv', ['wiki_compile_worker.py', '--cwd', str(self.root),
                                        '--json', '--flush-all']), redirect_stdout(out):
            self.assertEqual(worker.main(), 0)
        return json.loads(out.getvalue())

    def overlapping_worker_extractions(self):
        """A posttool edit is mid-extraction when a second trigger starts a worker.

        The second worker runs inside the first one's paid call: the same kernel
        flock conflict two processes would see, without threads or children.
        """
        queue = self.root / '.auto-context' / 'compile' / 'source-queue.jsonl'
        pending = wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude')
        job = {**self.job('post_tool_source'),
               'pendingRefresh': {key: pending[key] for key in ('eventId', 'ts', 'engine')}}
        queue.write_text(json.dumps(job) + '\n', encoding='utf-8')
        nested = []

        def extractor(argv, payload, timeout, root):
            self.extractor_calls.append(payload['trigger'])
            if len(self.extractor_calls) == 1:
                nested.append(self.run_main())  # e.g. laya-compile wake beside a kick
            return {'candidates': [{'title': 'Source decision', 'summary': 'Cards cite source.'}]}, None, 0

        def compile_echo(root, candidate):
            result = self.fake_compile(root, candidate)
            result['sourceRefreshEventId'] = candidate.get('sourceRefreshEventId')
            return result
        with patch('setup_guard.status', return_value={'status': 'ready'}), \
             patch.object(worker, '_run_verify_pass'), \
             patch.object(worker, 'run_extractor', side_effect=extractor), \
             patch.object(worker, 'compile_candidate', side_effect=compile_echo):
            first = self.run_main()
            after = self.run_main()  # a later wake once the owner finished
        return queue, nested, first, after

    def test_overlapping_worker_does_not_reextract_inflight_pending_revision(self):
        queue, nested, first, after = self.overlapping_worker_extractions()
        self.assertEqual(self.extractor_calls, ['post_tool_source'])
        # Busy without recovery; the in-flight event is still unresolved, so the
        # kick path is asked for one retry instead of losing its wake.
        self.assertEqual(nested, [{'processed': 0, 'remaining': 0, 'busy': True,
                                   'wakeAfterSeconds': worker.RUN_LOCK_BUSY_RETRY_SECS}])
        self.assertEqual(first['processed'], 1)
        self.assertEqual(after['processed'], 0)
        self.assertEqual(queue.read_text(encoding='utf-8'), '')
        self.assertEqual(wiki_freshness.unresolved_pending_refreshes(self.root), [])

    def test_overlapping_worker_without_run_lock_pays_twice(self):
        # Control for the test above: the pre-lock behavior really re-extracts.
        with patch.object(worker, 'try_run_lock',
                          side_effect=lambda _root: os.open(os.devnull, os.O_RDONLY)):
            _queue, nested, _first, _after = self.overlapping_worker_extractions()
        self.assertEqual(nested[0]['processed'], 1)  # the overlap recovered E1 and paid again
        self.assertGreaterEqual(len(self.extractor_calls), 2)

    def test_busy_worker_keeps_rows_and_requests_one_retry(self):
        queue = self.root / '.auto-context' / 'compile' / 'source-queue.jsonl'
        queue.write_text(json.dumps(self.job('session_start')) + '\n', encoding='utf-8')
        owner = worker.try_run_lock(self.root)
        self.assertIsNotNone(owner)
        try:
            with patch('setup_guard.status', return_value={'status': 'ready'}):
                report = self.run_main()
        finally:
            os.close(owner)
        self.assertEqual(report['wakeAfterSeconds'], worker.RUN_LOCK_BUSY_RETRY_SECS)
        self.assertEqual(self.extractor_calls, [])
        self.assertEqual(len(queue.read_text(encoding='utf-8').splitlines()), 1)
        # Lock released (as on owner exit or crash): the retained row now runs.
        with patch('setup_guard.status', return_value={'status': 'ready'}), \
             patch.object(worker, '_run_verify_pass'):
            self.assertEqual(self.run_main()['processed'], 1)
        self.assertEqual(len(self.extractor_calls), 1)

    def test_run_lock_rejects_hardlink_and_loose_mode(self):
        base = self.root / '.auto-context' / 'compile'
        original = base / 'original.lock'
        original.write_text('owned', encoding='utf-8')
        lock = base / worker.COMPILE_RUN_LOCK
        os.link(original, lock)
        with self.assertRaises(OSError):
            worker.try_run_lock(self.root)
        self.assertEqual(original.read_text(encoding='utf-8'), 'owned')
        lock.unlink()
        original.chmod(0o644)
        os.rename(original, lock)
        with self.assertRaises(OSError):
            worker.try_run_lock(self.root)

    def test_legacy_route_keeps_existing_path(self):
        self.compile_verified_once()
        self.bound = None
        job = self.job('session_start')
        del job['selectedRoute']
        worker.process_job(self.root, self.config, self.config['compile'], job)
        self.assertEqual(len(self.extractor_calls), 2)


if __name__ == '__main__':
    unittest.main()
