"""Selected compile pays once per authoritative source revision."""
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import os
import subprocess
import sys
import tracemalloc

from memvane.settings import api as settings
from memvane.wiki.compile import wiki_compile as wc, wiki_compile_worker as worker
from memvane.wiki.verify import api as verification_stamp
from memvane.wiki.card import api as wiki_freshness, api as ys
from memvane.wiki.workspace import api as wiki_selected_laya
from memvane.wiki.intake import api as wiki_compile_enqueue
from tests.support.wiki_handoff import verify_rows
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import use_config
from tests.support.queue_files import pending_text, replace_queue


class RevisionSkipChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.docs = self.root / 'docs'
        self.wiki = self.root / '.memvane' / 'wiki'
        self.docs.mkdir(parents=True)
        self.wiki.mkdir(parents=True, exist_ok=True)
        (self.root / '.memvane' / 'compile').mkdir(parents=True)
        self.source = self.docs / 'source.md'
        self.source.write_text('# Source\n\nDurable decision: cards cite source.\n', encoding='utf-8')
        self.config = {
            'indexing': True,
            'collections': ['proj-docs', 'proj-wiki'],
            'collectionPaths': {'proj-docs': 'docs', 'proj-wiki': '.memvane/wiki'},
            'collectionRoles': {'proj-docs': 'raw', 'proj-wiki': 'wiki'},
            'wikiPath': '.memvane/wiki',
            'compile': {
                'mode': 'auto-wiki', 'defaultStatus': 'generated',
                'triggers': ['post_tool_source', 'manual'], 'maxSourceChars': 12000,
                'extractor': {'builtins': ['claude'], 'timeout': 30},
            },
        }
        use_config(self, self.project, self.config)
        self.decision = {
            'state': 'candidate', 'projectRoot': str(self.root),
            'installId': 'a' * 32, 'generation': 'g-' + 'b' * 32,
            'scopeId': 'c' * 32, 'storeIncarnation': 'd' * 32,
            'collections': [
                {'name': 'proj-docs', 'role': 'raw', 'sourceRoot': str(self.docs)},
                {'name': 'proj-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)},
            ],
            'collectionRoots': {'proj-docs': str(self.docs), 'proj-wiki': str(self.wiki)},
            'recallSettings': {'skipPaths': []},
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

    def review(self, quote):
        from tests.support.engines_fake import FakeEngines
        verifier = FakeEngines({'verify': {'verdict': 'pass', 'reasons': [], 'claims': [
            {'claim': quote, 'supported': True, 'quote': quote, 'sourcePath': 'docs/source.md'}]}}).install(self)
        result = verification_stamp.run(self.root, self.config, self.config['compile'])
        return result, [call['task'] for call in verifier.calls]

    def verify(self, card):
        body_hash = ys.card_body_hash(card.read_text(encoding='utf-8'))
        self.assertTrue(verification_stamp.stamp_verification(card, 'verified', 'codex', 'cross-engine', body_hash))

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

    def rename_raw(self):
        """Minimal migration: raw `proj-docs` at docs/ becomes `proj` at the root."""
        self.config = {**self.config, 'collections': ['proj', 'proj-wiki'],
                       'collectionPaths': {'proj': '.', 'proj-wiki': '.memvane/wiki'},
                       'collectionRoles': {'proj': 'raw', 'proj-wiki': 'wiki'}}
        use_config(self, self.project, self.config)
        self.decision.update({
            'collections': [{'name': 'proj', 'role': 'raw', 'sourceRoot': str(self.root)},
                            {'name': 'proj-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}],
            'collectionRoots': {'proj': str(self.root), 'proj-wiki': str(self.wiki)}})

    def renamed_job(self, trigger):
        job = self.job(trigger)
        job['source'] = {**job['source'], 'collection': 'proj'}
        job['selectedRoute'] = wiki_selected_laya.route_identity(self.bound)
        return job

    def test_five_edits_extract_once_and_replayed_revision_waits_for_pending_review(self):
        from memvane.wiki.compile import refresh_settlement

        def write(root, candidate):
            out = io.StringIO()
            with redirect_stdout(out):
                wc._compile_locked(root, self.config, self.config['compile'], self.wiki,
                                   candidate)
            return json.loads(out.getvalue())

        self.config['compile']['semanticDedup'] = {'enabled': False}
        queue = wiki_selected_laya.safe_queue_path(self.root)
        live = self.wiki / 'concepts/source-decision.md'
        with patch.object(worker, 'compile_candidate', side_effect=write), \
             patch.object(wiki_selected_laya, 'request_sync'), \
             patch.object(wiki_selected_laya, 'request_compile'):
            self.process('post_tool_source')
            initial, calls = self.review('Durable decision')
            self.assertEqual((initial['remaining'], calls), (0, ['verify']))
            self.extractor_calls.clear()
            for i in range(5):
                self.source.write_text(f'Synthetic source decision {i}\n')
                wiki_compile_enqueue.enqueue_payload({'hook_event_name': 'PostToolUse',
                    'cwd': str(self.root), 'engine': 'claude', 'tool_input': {'file_path': str(self.source)}})
            rows = [json.loads(line) for line in pending_text(queue).splitlines()]
            self.assertEqual(len(rows), 1)
            with patch.object(worker, '_run_verify_pass'):
                self.assertEqual(self.run_main()['completed'], 1)
            self.assertFalse(pending_text(queue))
            self.assertEqual(self.extractor_calls, ['post_tool_source'])
            from memvane.wiki.pipeline import api as pipeline
            first_row, = verify_rows(self.root)
            first = first_row['payload']
            self.assertEqual(first['kind'], 'replace')
            event = wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude')
            # A live verifier holds the row while the same revision is queued again.
            with pipeline.open_pipeline(self.root) as store:
                store.claim_verify_batch(os.getpid(), 'live-review', budget=1)
            retry = dict(rows[0], pendingRefresh={k: event[k] for k in ('eventId', 'ts', 'engine')})
            self.assertNotEqual(retry['pendingRefresh']['eventId'], rows[0]['pendingRefresh']['eventId'])
            wiki_compile_enqueue.append_source_jobs(queue, [retry])
            with patch.object(worker, '_run_verify_pass'):
                self.assertEqual(self.run_main()['completed'], 1)
            self.assertEqual(self.extractor_calls, ['post_tool_source'])
            self.assertEqual(refresh_settlement.unsettled(self.root, [event]), [])
            with pipeline.open_pipeline(self.root) as store:
                self.assertEqual(store.release_verify_claim('live-review'), 1)
            self.assertEqual(wiki_freshness.latest_unresolved_pending(self.root, 'docs/source.md'), event)
            self.source.write_text('Synthetic source after review was queued\n')
            self.process('post_tool_source')
            second = verify_rows(self.root)[-1]['payload']
            self.assertEqual(len(self.extractor_calls), 2)
            self.assertNotEqual(second['sourceRevisions'], first['sourceRevisions'])
            self.assertEqual(list(live.parent.glob('.*')), [])
            result, calls = self.review('Synthetic source')
            self.assertEqual((result['processed'], result['superseded'], result['remaining'], calls),
                             (1, 1, 0, ['verify']))
            self.assertIn(second['sourceRevisions'][0]['sha256'], live.read_text())
            self.assertNotIn(first['sourceRevisions'][0]['sha256'], live.read_text())
            self.assertEqual(verify_rows(self.root), [])
            self.assertEqual(self.review('Synthetic source')[1], [])

    def test_nested_meta_named_card_is_compile_proof(self):
        # Only wiki-root SCHEMA.md/index.md/log.md are meta pages. A verified card
        # at concepts/index.md proves its revision like any other card.
        self.compile_verified_once()
        revision = self.revisions[0][0]
        for name in ('SCHEMA.md', 'index.md'):
            nested = self.cards[0].with_name(name)
            self.cards[0].rename(nested)
            self.cards[0] = nested
            self.assertEqual(worker.selected_compiled_revision_card(
                self.root, self.bound, revision), 'concepts/' + name)
            self.process('session_start')
            self.assertEqual(len(self.extractor_calls), 1, name)
        # The same card at the wiki root is a leftover meta page, never proof.
        root_page = self.wiki / 'index.md'
        self.cards[0].rename(root_page)
        self.assertIsNone(worker.selected_compiled_revision_card(
            self.root, self.bound, revision))

    def test_old_label_card_proves_the_revision_after_a_raw_rename(self):
        self.compile_verified_once()
        self.rename_raw()
        done = worker.process_job(self.root, self.config, self.config['compile'],
                                  self.renamed_job('session_start'))
        self.assertEqual(done, (True, False, []))
        self.assertEqual(self.extractor_calls, ['post_tool_source'])
        self.assertEqual(len(self.cards), 1)
        self.assertEqual([row['card'] for row in self.skip_reasons()], ['concepts/card-1.md'])

    def test_wiki_label_with_the_source_path_is_not_proof(self):
        self.compile_verified_once()
        card = self.cards[0]
        card.write_text(card.read_text(encoding='utf-8').replace(
            'collection: "proj-docs", sha256', 'collection: "proj-wiki", sha256'),
            encoding='utf-8')
        self.verify(card)
        self.assertEqual(self.process('session_start'), (True, False, []))
        self.assertEqual(len(self.extractor_calls), 2)

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
        queue = self.root / '.memvane' / 'compile' / 'source-queue.jsonl'
        with patch.object(worker, '_run_verify_pass'):
            for trigger in ('session_start', 'manual'):
                replace_queue(queue, json.dumps(self.job(trigger)) + '\n')
                out = io.StringIO()
                with patch.object(sys, 'argv', ['wiki_compile_worker.py', '--cwd', str(self.root),
                                                '--json', '--flush-all']), redirect_stdout(out):
                    self.assertEqual(worker.main(), 0)
                self.assertEqual(json.loads(out.getvalue())['processed'], 1)
                self.assertEqual(pending_text(queue), '')
        self.assertEqual(len(self.extractor_calls), 1)

        # Changed bytes are a new revision: one more paid extraction, then idempotent again.
        self.source.write_text('# Source\n\nDurable decision: cards cite the source.\n', encoding='utf-8')
        self.process('file_change')
        self.assertEqual(len(self.extractor_calls), 2)
        self.verify(self.cards[1])
        self.process('session_start')
        self.assertEqual(len(self.extractor_calls), 2)

    def test_selected_excluded_source_folder_skips_old_queued_job_before_model(self):
        excluded = self.docs / 'build' / 'source.md'
        excluded.parent.mkdir()
        excluded.write_text('# Excluded\n', encoding='utf-8')
        self.decision['recallSettings']['skipPaths'] = ['build']
        job = self.job('post_tool_source')
        job['source']['path'] = 'docs/build/source.md'
        self.assertEqual(worker.process_job(
            self.root, self.config, self.config['compile'], job), (True, False, []))
        self.assertEqual(self.extractor_calls, [])
        ledger = worker.candidate_path(self.root, self.config['compile'])
        self.assertEqual(json.loads(ledger.read_text().splitlines()[0])['reason'],
                         'source_folder_excluded')

    def test_selected_source_enqueue_checks_parent_folder_not_filename(self):
        folder = self.docs / 'build'
        folder.mkdir()
        excluded = folder / 'source.md'
        included = self.docs / 'build-notes.md'
        excluded.write_text('# Excluded\n', encoding='utf-8')
        included.write_text('# Included\n', encoding='utf-8')
        self.decision['recallSettings']['skipPaths'] = ['build']
        self.assertIsNone(wiki_compile_enqueue.source_record(
            str(excluded), str(self.root), str(self.root), self.config, 'claude'))
        record = wiki_compile_enqueue.source_record(
            str(included), str(self.root), str(self.root), self.config, 'claude')
        self.assertEqual(record['source']['path'], 'docs/build-notes.md')

    def test_claim_consumes_force_backfill_and_edit_independently(self):
        self.compile_verified_once()
        self.decision['storePath'] = str(self.home / 'store.sqlite')
        self.extractor_calls.clear()
        revision = self.revisions[0][0]['sha256']
        queue = wiki_selected_laya.safe_queue_path(self.root)
        jobs = [dict(self.job('post_sync_source'), forceRecompile=True, contentRevision=revision),
                dict(self.job('session_start_source'), initialBackfill='f' * 32, contentRevision=revision),
                self.job('post_tool_source')]
        wiki_compile_enqueue.append_source_jobs(queue, jobs)
        self.assertEqual(len(pending_text(queue).splitlines()), 3)
        with patch.object(worker, '_run_verify_pass'), patch.object(wiki_compile_enqueue, 'settle_job') as settled, \
                patch.object(worker, 'compile_candidate', return_value={'action': 'unchanged'}):
            result = self.run_main()
        self.assertEqual((result['processed'], result['completed'], result['remaining']), (3, 3, 0))
        self.assertEqual([(call.args[1].get('forceRecompile'), call.args[1].get('initialBackfill'))
                          for call in settled.call_args_list], [(True, None), (None, 'f' * 32), (None, None)])
        self.assertEqual(self.extractor_calls, ['post_sync_source'])
        self.assertFalse(queue.exists() and queue.stat().st_size)

    def test_source_edit_waits_for_claim_without_repeating_extraction(self):
        cfg = self.config['compile']
        cfg['semanticDedup'] = {'enabled': False}
        verify = self.root / '.memvane/compile/verify-queue.jsonl'
        source_queue = verify.with_name('source-queue.jsonl')
        live = self.wiki / 'concepts/source-decision.md'

        def write(root, card):
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = wc._compile_locked(root, self.config, cfg, self.wiki, card)
            return json.loads(stream.getvalue() or '{}') if code == 0 else None

        with patch.object(worker, 'compile_candidate', side_effect=write), \
                patch.object(wiki_selected_laya, 'request_sync'), patch.object(wiki_selected_laya, 'request_compile'), \
                patch.object(worker, '_run_verify_pass'):
            self.process('post_tool_source')
            self.verify(live)
            verify.write_text('')
            self.source.write_text('Intermediate content.')
            self.process('post_tool_source')
            from memvane.wiki.pipeline import api as pipeline
            held = [row for row in verify_rows(self.root) if row['payload']['kind'] == 'replace'][-1]
            with pipeline.open_pipeline(self.root) as store:  # a live verifier owns the rows
                store.claim_verify_batch(os.getpid(), 'live-review', budget=10)
            unrelated = self.project.write_note('docs/another.md', 'A different source.')
            job = self.job('post_tool_source')
            job['source']['path'] = unrelated.relative_to(self.root).as_posix()
            with patch.object(worker, 'run_extractor', return_value=({'candidates': []}, None, 0)) as other:
                worker.process_job(self.root, self.config, cfg, job)
            self.assertEqual(other.call_count, 1)
            self.extractor_calls.clear()
            for text in ('Latest content.', 'Latest content.', 'Another revision during verification.'):
                self.source.write_text(text)
                wiki_compile_enqueue.enqueue_payload({'hook_event_name': 'PostToolUse', 'cwd': str(self.root),
                    'engine': 'claude', 'tool_input': {'file_path': str(self.source)}})
                result = self.run_main()
                self.assertEqual((result['remaining'], result['completed']), (1, 0))
                self.assertFalse(source_queue.exists() and source_queue.stat().st_size)
            self.assertIn(held['payload'], [row['payload'] for row in verify_rows(self.root)])
            self.assertEqual(self.extractor_calls, [])
            with pipeline.open_pipeline(self.root) as store:
                self.assertGreaterEqual(store.release_verify_claim('live-review'), 1)
            result = self.run_main()
            self.assertEqual((result['remaining'], result['completed']), (0, 1))
            self.assertEqual(self.extractor_calls, ['post_tool_source'])
            digest = self.project.revision(self.source)['sha256']
            active = verify_rows(self.root)[-1]['payload']
            self.assertEqual([rev['sha256'] for rev in active['sourceRevisions']], [digest])

    def test_partial_retry_survives_resave_and_recovered_claim(self):
        cfg = self.config['compile']
        cfg['semanticDedup'] = {'enabled': False}
        queue = self.root / '.memvane/compile/source-queue.jsonl'
        target = self.wiki / 'concepts/source-decision.md'
        siblings = []

        def write(root, candidate):
            if candidate['title'] == 'Sibling':
                siblings.append(candidate['title'])
                if len(siblings) == 1:
                    return {'action': 'rejected'}
            out = io.StringIO()
            with redirect_stdout(out):
                wc._compile_locked(root, self.config, cfg, self.wiki, candidate)
            return json.loads(out.getvalue() or '{}')

        def edit():
            wiki_compile_enqueue.enqueue_payload({'hook_event_name': 'PostToolUse', 'cwd': str(self.root),
                'engine': 'claude', 'tool_input': {'file_path': str(self.source)}})

        with patch.object(worker, 'compile_candidate', side_effect=write), \
                patch.object(wiki_selected_laya, 'request_sync'), patch.object(wiki_selected_laya, 'request_compile'), \
                patch.object(worker, '_run_verify_pass'):
            self.process('post_tool_source')
            initial, calls = self.review('Durable decision')
            self.assertEqual((initial['remaining'], calls), (0, ['verify']))
            self.source.write_text('changed source\n')
            edit()
            two = {'candidates': [{'title': 'Source decision', 'summary': 'Cards cite source.'},
                                  {'title': 'Sibling', 'summary': 'Additional fact.'}]}
            with patch.object(worker, 'run_extractor', return_value=(two, None, 0)) as extract:
                first = self.run_main()
                self.assertEqual(first['remaining'], 1)
                from tests.support.wiki_handoff import claimed_jobs
                old, = [job for _, job in wiki_selected_laya.read_queue(queue)]
                revision = self.project.revision(self.source)['sha256']
                self.assertEqual((old['partialCompile'], old['_processedContentRevision']), (True, revision))
                held, = verify_rows(self.root)
                self.assertEqual(held['payload']['kind'], 'replace')

                # A real consumer exits after claiming the partial row and before acknowledging it.
                command = ('import sys; from pathlib import Path; from memvane.wiki.workspace import api; '
                           'print(api.claim_queue(Path(sys.argv[1])).token)')
                subprocess.check_output([sys.executable, '-B', '-c', command, str(queue)],
                                        cwd=Path(__file__).resolve().parents[5], text=True)
                self.assertEqual(claimed_jobs(queue), [old])
                self.source.write_bytes(self.source.read_bytes())
                edit()
                queued = [job for _, job in wiki_selected_laya.read_queue(queue) if job != old]
                current, = queued
                self.assertNotEqual(old['pendingRefresh']['eventId'], current['pendingRefresh']['eventId'])
                self.assertNotIn('partialCompile', current)
                self.assertEqual(claimed_jobs(queue), [old])
                self.assertEqual(extract.call_count, 1)
                # The next worker returns the dead claim and resumes only the unwritten sibling.
                retry = self.run_main()
                self.assertEqual(claimed_jobs(queue), [])
                for _ in range(3):
                    if not wiki_selected_laya.read_queue(queue):
                        break
                    self.run_main()
                self.assertEqual(wiki_selected_laya.read_queue(queue), [])
                self.assertEqual(verify_rows(self.root)[0], held)
                sibling = target.parent / 'sibling.md'
                self.assertIn(revision, sibling.read_text())
                self.assertEqual(extract.call_count, 2)
            # The resumed pass queued the target again; the older row is superseded at the claim.
            self.assertEqual([row['payload']['kind'] for row in verify_rows(self.root)], ['replace', 'replace', 'review'])
            verified, calls = self.review('changed source')
            self.assertEqual((verified['processed'], verified['superseded'], verified['remaining'], calls),
                             (2, 1, 0, ['verify', 'verify']))
            for card in (target, sibling):
                self.assertIn(revision, card.read_text())
                self.assertEqual(ys.parse_frontmatter(card.read_text())[0]['status'], 'verified')
            self.assertIn(verify_rows(self.root, states=('done',))[-2]['payload']['sourceHash'], target.read_text())
            replay, calls = self.review('changed source')
            self.assertEqual((replay['processed'], replay['remaining'], calls), (0, 0, []))

    def test_old_partial_retry_does_not_force_a_different_verified_revision(self):
        self.compile_verified_once()
        retry = dict(self.job('post_tool_source'), partialCompile=True, _processedContentRevision='0' * 64)
        self.assertEqual(worker.process_job(self.root, self.config, self.config['compile'], retry), (True, False, []))
        self.assertEqual(self.extractor_calls, ['post_tool_source'])

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

    def test_pending_partial_card_cannot_resolve_event_from_sibling_job(self):
        self.assertIsNotNone(wiki_freshness.record_pending_refresh(
            self.root, 'docs/source.md', 'claude'))
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
            first = self.job('post_tool_source')
            self.assertEqual(worker.process_job(
                self.root, self.config, self.config['compile'], first)[:2],
                (False, True))
            self.verify(self.cards[0])
            self.assertIsNotNone(wiki_freshness.latest_unresolved_pending(
                self.root, 'docs/source.md'))
            sibling = self.job('session_start')  # no partialCompile flag
            snapshot = wiki_freshness.snapshot_bytes(self.source)[0]
            revision = {'kind': 'file', 'path': 'docs/source.md',
                        'collection': 'proj-docs', **snapshot}
            self.assertIsNone(worker.selected_compiled_revision_card(
                self.root, self.bound, revision, content_only=True))
            outcome = worker.process_job(
                self.root, self.config, self.config['compile'], sibling)
            self.assertEqual(outcome[:2], (True, False))
        self.assertEqual(len(self.extractor_calls), 2)
        self.assertIn('B', calls[2:])
        self.assertEqual(self.skip_reasons(), [])

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

    def test_mtime_only_change_reuses_verified_card_but_tamper_does_not(self):
        self.compile_verified_once()
        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.process('session_start')
        self.assertEqual(len(self.extractor_calls), 1)
        card = self.cards[0]
        card.write_text(card.read_text(encoding='utf-8') + 'tampered\n', encoding='utf-8')
        self.process('manual')  # verifiedBodyHash no longer matches
        self.assertEqual(len(self.extractor_calls), 2)

    def test_same_content_pending_refresh_is_resolved_without_extraction(self):
        self.compile_verified_once()
        self.assertIsNotNone(wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude'))
        self.process('post_tool_source')
        self.assertEqual(len(self.extractor_calls), 1)
        self.assertEqual(len(self.skip_reasons()), 1)
        self.assertIsNone(wiki_freshness.latest_unresolved_pending(self.root, 'docs/source.md'))

    def test_changed_content_pending_refresh_still_extracts(self):
        self.compile_verified_once()
        self.source.write_text('# Source\nA new decision requires a wiki update.\n', encoding='utf-8')
        self.assertIsNotNone(wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude'))
        self.process('post_tool_source')
        self.assertEqual(len(self.extractor_calls), 2)
        self.assertEqual(self.skip_reasons(), [])

    def test_corrupt_pending_ledger_does_not_skip(self):
        self.compile_verified_once()
        wiki_freshness.unresolved_pending_refreshes(self.root, compact=True)
        inbox = settings.ledger(self.root, 'edit-inbox.jsonl')
        inbox.symlink_to(self.home / 'elsewhere.jsonl')  # an unreadable inbox is unknown, not empty
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
        per_card = worker.SELECTED_PROOF_MAX_CARD_BYTES
        used = self.cards[0].stat().st_size
        fillers = []
        while budget - used > per_card:
            filler = self.sparse_card(f'notes/filler-{len(fillers)}.md', per_card)
            fillers.append(filler)
            used += per_card
        remaining = budget - used
        # Exactly the remaining aggregate budget still proves the revision.
        filler = self.sparse_card('notes/last-filler.md', remaining)
        self.assertEqual(worker.selected_compiled_revision_card(self.root, self.bound, revision),
                         'concepts/card-1.md')
        # One byte past the remaining aggregate (each card under the per-file cap).
        with open(filler, 'r+b') as out:
            out.truncate(remaining + 1)
        card, peak = self.proof_peak(revision)
        self.assertIsNone(card)
        self.assertLess(peak, per_card * 3)
        # One card alone past the per-file cap.
        filler.unlink()
        for item in fillers:
            item.unlink()
        self.sparse_card('notes/huge.md', per_card + 1)
        card, peak = self.proof_peak(revision)
        self.assertIsNone(card)
        self.assertLess(peak, per_card * 3)
        # The real worker keeps the paid path instead of skipping.
        self.process('session_start')
        self.assertEqual(len(self.extractor_calls), 2)
        self.assertEqual(self.skip_reasons(), [])

    def test_bounded_proof_can_cover_more_than_200_wiki_cards(self):
        self.compile_verified_once()
        revision = self.revisions[0][0]
        for n in range(210):
            self.sparse_card(f'notes/other-{n:03}.md', 0)
        self.assertEqual(worker.selected_compiled_revision_card(
            self.root, self.bound, revision), 'concepts/card-1.md')
        with patch.object(worker, 'SELECTED_PROOF_MAX_CARDS', 200):
            self.assertIsNone(worker.selected_compiled_revision_card(
                self.root, self.bound, revision))

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
        queue = self.root / '.memvane' / 'compile' / 'source-queue.jsonl'
        pending = wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude')
        job = {**self.job('post_tool_source'),
               'pendingRefresh': {key: pending[key] for key in ('eventId', 'ts', 'engine')}}
        replace_queue(queue, json.dumps(job) + '\n')
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
        with patch.object(worker, '_run_verify_pass'), \
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
        self.assertEqual(pending_text(queue), '')
        self.assertEqual(wiki_freshness.unresolved_pending_refreshes(self.root), [])

    def test_busy_worker_keeps_rows_and_requests_one_retry(self):
        queue = self.root / '.memvane' / 'compile' / 'source-queue.jsonl'
        replace_queue(queue, json.dumps(self.job('session_start')) + '\n')
        owner = worker.try_run_lock(self.root)
        self.assertIsNotNone(owner)
        try:
            report = self.run_main()
        finally:
            os.close(owner)
        self.assertEqual(report['wakeAfterSeconds'], worker.RUN_LOCK_BUSY_RETRY_SECS)
        self.assertEqual(self.extractor_calls, [])
        self.assertEqual(len(pending_text(queue).splitlines()), 1)
        # Lock released (as on owner exit or crash): the retained row now runs.
        with patch.object(worker, '_run_verify_pass'):
            self.assertEqual(self.run_main()['processed'], 1)
        self.assertEqual(len(self.extractor_calls), 1)

    def test_run_lock_rejects_hardlink_and_loose_mode(self):
        base = self.root / '.memvane' / 'compile'
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


    def test_shared_delete_schedules_survivor_once_and_retains_wait_marker(self):
        from memvane.wiki.lifecycle import api as wiki_source_retire
        gone = self.docs / 'gone.md'
        gone.write_text('old deleted bytes', encoding='utf-8')
        old_hash = wiki_freshness.snapshot_bytes(gone)[0]['sha256']
        gone.unlink()
        job = self.job('file_change')
        job['source']['path'] = 'docs/gone.md'
        job['contentRevision'] = old_hash
        job['sourceDelete'] = True
        queue = wiki_selected_laya.safe_queue_path(self.root)
        result = {'state': 'shared_pending',
                  'survivors': [('proj-docs', 'docs/source.md')]}
        with patch.object(wiki_source_retire, 'retire_deleted_source', return_value=result), \
             patch.object(wiki_selected_laya, 'request_compile') as wake:
            self.assertEqual(worker.process_job(
                self.root, self.config, self.config['compile'], job),
                (True, False, []))
            rows = [json.loads(line) for line in pending_text(queue).splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]['forceRecompile'])
            self.assertEqual(rows[0]['source']['path'], 'docs/source.md')
            self.assertTrue(rows[1]['sourceDelete'])
            self.assertTrue(rows[1]['sharedEnqueued'])
            self.assertEqual(wake.call_count, 1)
            self.assertEqual(worker.process_job(
                self.root, self.config, self.config['compile'], rows[1]),
                (False, True, []))
            self.assertEqual(wake.call_count, 1,
                             'a held deletion must not repeat a paid refresh')
        self.assertEqual(self.extractor_calls, [])

    def test_forced_survivor_refresh_drops_model_suggested_deleted_source(self):
        captured = []
        def extractor(*_args):
            return {'candidates': [{
                'title': 'Survivor', 'summary': 'Current source only',
                'sources': [{'kind': 'file', 'path': 'docs/gone.md',
                             'collection': 'proj-docs'}],
            }]}, None, 0
        def compile_card(root, candidate):
            captured.append(candidate)
            return self.fake_compile(root, candidate)
        job = self.job('file_change')
        job['forceRecompile'] = True
        with patch.object(worker, 'run_extractor', side_effect=extractor), \
             patch.object(worker, 'compile_candidate', side_effect=compile_card):
            self.assertEqual(worker.process_job(
                self.root, self.config, self.config['compile'], job),
                (True, False, []))
        self.assertEqual(captured[0]['sources'], [
            {'kind': 'file', 'path': 'docs/source.md', 'collection': 'proj-docs'}])
