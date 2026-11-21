"""A compile attempt that ends without a card is not repeated on every worker pass.

A PostToolUse edit records a refresh event that only a card proving the
edited revision resolves. Four endings leave the event unresolved for good:
the writer keeps a card it does not own (merge-needed), the verifier
suppressed that exact content, the source is gone, the extractor returns no
candidate. Each used to be recovered and paid for again on every pass while
the selected execution reported ``compile_progress`` and the wake came back in
30 seconds without counting.

Selected fixture project, a fake extractor and a fake writer answer; every
assertion is a count of extractor calls, audit rows, queue rows or events.
"""
import argparse
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from memvane.wiki.card import api as wiki_freshness
from memvane.wiki.compile import refresh_settlement
from memvane.wiki.compile import selected_execution as execution
from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.engines import api as engines
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config

SOURCE = 'docs/canoe-route.md'
PASSES = 4


class CompileLoop(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.project.write_note(SOURCE, 'Synthetic source for the compile loop.\n')
        self.project.git('add', '--', '.memvane/settings.json', 'docs')
        self.project.git('commit', '-qm', 'fixture')
        build_install(self, self.project)
        self.config = expanded(self.project, compile={
            'budget': {'extractorPerRun': 10},
            'extractor': {'builtins': ['claude']}, 'verify': {'enabled': False}})
        use_config(self, self.project, self.config)
        self.root = self.project.root
        self.bound = workspace.selected(self.root)
        self.compile_dir = self.root / '.memvane/compile'
        self.compile_dir.mkdir(parents=True, exist_ok=True)
        self.queue = self.compile_dir / 'source-queue.jsonl'
        self.verify_queue = self.compile_dir / 'verify-queue.jsonl'
        self.calls = []
        self.answer = {'candidates': []}
        self.writer = None

    # -- driving --------------------------------------------------------------------
    def edit(self, text=None):
        """What a PostToolUse edit leaves: new bytes (optional) and one refresh event."""
        if text is not None:
            (self.root / SOURCE).write_text(text, encoding='utf-8')
        event = wiki_freshness.record_pending_refresh(self.root, SOURCE, 'claude')
        self.assertIsNotNone(event)
        return event

    def extract(self, argv, payload, timeout, root):
        self.calls.append(payload['source']['path'])
        return json.loads(json.dumps(self.answer)), None, 0

    def worker_pass(self):
        """One worker pass; returns its JSON report."""
        output = io.StringIO()
        patches = [patch.object(engines, 'run_extractor', side_effect=self.extract),
                   patch.object(worker, 'gather_similar_pages', return_value=[]),
                   patch.object(worker, '_run_verify_pass'),
                   patch.object(workspace, 'request_compile')]
        if self.writer is not None:
            patches.append(patch.object(worker, 'compile_candidate', side_effect=self.writer))
        for item in patches:
            item.start()
        try:
            with redirect_stdout(output):
                worker._main_locked(argparse.Namespace(json=True, flush_all=True), self.root,
                                    self.config, self.config['compile'], self.queue, self.bound)
        finally:
            for item in reversed(patches):
                item.stop()
        return json.loads(output.getvalue())

    def selected_pass(self):
        """``selected_execution.run`` with the worker child run in this process."""
        real = execution.subprocess.run

        def child(argv, **kwargs):
            # Only the worker child is replaced; git and every other process run as they are.
            if 'wiki-compile-worker' not in argv:
                return real(argv, **kwargs)
            with patch.object(execution.subprocess, 'run', real):
                report = self.worker_pass()
            return SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr='')
        with patch.object(execution.subprocess, 'run', side_effect=child), \
                patch.object(workspace, 'claim_lifecycle', return_value=None):
            return execution.run(self.root)

    # -- observation ----------------------------------------------------------------
    def audit(self, reason=None):
        path = self.compile_dir / 'candidates.jsonl'
        rows = ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
                if path.exists() else [])
        return [row for row in rows if reason is None or row.get('reason') == reason]

    def queued(self):
        files = [self.queue] + list(self.compile_dir.glob('source-queue.jsonl.claimed.*'))
        return [line for path in files if path.exists()
                for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def settled(self):
        path = self.compile_dir / refresh_settlement.SETTLED_NAME
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def assert_attempted_once(self, event, *, calls, reason):
        """After several more passes nothing is attempted again and the wake drains."""
        ledger = (self.compile_dir / 'source-refresh-pending.jsonl').read_bytes()
        states = [self.selected_pass()['state'] for _ in range(PASSES)]
        self.assertEqual(states, ['drained'] * PASSES)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.queued(), [])
        self.assertEqual(list(self.settled()), [event['eventId']])
        self.assertEqual(self.settled()[event['eventId']]['reason'], reason)
        # Recall freshness still reads the event as unresolved, and the ledger bytes are as they were.
        self.assertEqual([item['eventId'] for item in wiki_freshness.unresolved_pending_refreshes_strict(self.root)],
                         [event['eventId']])
        self.assertEqual((self.compile_dir / 'source-refresh-pending.jsonl').read_bytes(), ledger)

    # -- the four endings -----------------------------------------------------------
    def test_an_extractor_answer_with_no_candidate_is_paid_for_once(self):
        event = self.edit()
        first = self.selected_pass()
        self.assertEqual((first['state'], len(self.calls)), ('drained', 1))
        self.assert_attempted_once(event, calls=1, reason='not_refreshed')

    def test_a_card_the_writer_does_not_own_is_paid_for_once(self):
        # The writer keeps a card without a managed block: merge-needed, no card written.
        def keep(root, candidate, **_kwargs):
            return {'action': 'merge-needed', 'lint': {'findings': ['managed_section_missing']},
                    'targetPath': '.memvane/wiki/concepts/canoe-route.md',
                    'sourceRevisions': candidate['sourceRevisions'],
                    'sourceRefreshEventId': candidate.get('sourceRefreshEventId')}
        self.writer = keep
        self.answer = {'candidates': [{'title': 'Canoe route', 'summary': 'Synthetic fact',
                                       'targetPath': '.memvane/wiki/concepts/canoe-route.md'}]}
        event = self.edit()
        self.assertEqual((self.selected_pass()['state'], len(self.calls)), ('drained', 1))
        self.assert_attempted_once(event, calls=1, reason='not_refreshed')

    def test_content_the_verifier_suppressed_is_skipped_once(self):
        event = self.edit()
        body = (self.root / SOURCE).read_text(encoding='utf-8')
        suppressed = worker.wiki_verify.source_body_hash(body)
        with patch.object(worker, 'verify_suppression_hash', return_value=suppressed):
            self.assertEqual(self.selected_pass()['state'], 'drained')
            self.assert_attempted_once(event, calls=0, reason='verify_inconclusive_suppressed')
        self.assertEqual(len(self.audit('verify_inconclusive_suppressed')), 1)

    def test_a_source_that_is_gone_is_looked_at_once(self):
        event = self.edit()
        (self.root / SOURCE).unlink()
        self.assertEqual(self.selected_pass()['state'], 'drained')
        self.assert_attempted_once(event, calls=0, reason='source_unreadable')
        self.assertEqual(len(self.audit('source_unreadable')), 1)
        self.assertIsNone(self.settled()[event['eventId']]['sourceSha256'])
        # The file coming back is new content: it is attempted.
        (self.root / SOURCE).write_text('Back again.\n', encoding='utf-8')
        self.selected_pass()
        self.assertEqual(len(self.calls), 1)

    # -- what still runs again --------------------------------------------------------
    def test_a_new_edit_of_the_same_source_is_attempted_again(self):
        first = self.edit()
        self.selected_pass()
        self.assertEqual(len(self.calls), 1)
        second = self.edit('Edited again.\n')
        self.assertNotEqual(second['eventId'], first['eventId'])
        self.assertEqual(self.selected_pass()['state'], 'drained')
        self.assertEqual(len(self.calls), 2)
        # Only the latest unresolved event of the source is kept.
        self.assertEqual(list(self.settled()), [second['eventId']])
        self.assert_attempted_once(second, calls=2, reason='not_refreshed')

    def test_changed_bytes_without_a_new_event_are_attempted_again(self):
        event = self.edit()
        self.selected_pass()
        (self.root / SOURCE).write_text('Changed outside the edit hook.\n', encoding='utf-8')
        self.assertEqual(self.selected_pass()['state'], 'drained')
        self.assertEqual(len(self.calls), 2)
        self.assert_attempted_once(event, calls=2, reason='not_refreshed')

    def test_a_card_that_proves_the_revision_resolves_the_event_and_settles_nothing(self):
        def written(root, candidate, **_kwargs):
            return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions'],
                    'sourceRefreshEventId': candidate.get('sourceRefreshEventId')}
        self.writer = written
        self.answer = {'candidates': [{'title': 'Canoe route', 'summary': 'Synthetic fact'}]}
        self.edit()
        self.assertEqual(self.selected_pass()['state'], 'drained')
        self.assertEqual((len(self.calls), self.settled()), (1, {}))
        self.assertEqual(wiki_freshness.unresolved_pending_refreshes_strict(self.root), [])

    def test_a_kept_job_is_not_settled_and_is_not_progress(self):
        # A cooling-down extractor keeps the job: the event stays to be attempted.
        event = self.edit()
        with patch.object(worker, 'cooldown_active', return_value=True):
            results = [self.selected_pass() for _ in range(3)]
        self.assertEqual([(r['state'], r.get('reason')) for r in results], [('pending', None)] * 3)
        self.assertEqual((len(self.calls), self.settled()), (0, {}))
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.selected_pass()['state'], 'drained')
        self.assert_attempted_once(event, calls=1, reason='not_refreshed')

    # -- without the settlement -------------------------------------------------------
    def test_without_the_settlement_every_pass_pays_again_and_reports_no_progress(self):
        """The defect, kept reproducible: ``process_job`` alone leaves the loop open."""
        self.edit()
        with patch.object(worker, 'process_job_settled', side_effect=worker.process_job):
            results = [self.selected_pass() for _ in range(PASSES)]
        self.assertEqual(len(self.calls), PASSES)
        self.assertEqual(self.settled(), {})
        # The backlog is the same after each pass: counted pending, never ``compile_progress``.
        self.assertEqual([(r['state'], r.get('reason')) for r in results], [('pending', None)] * PASSES)


    def test_a_settlement_that_cannot_be_written_leaves_counted_retries_not_progress(self):
        """The record is a cache: when it cannot be kept the attempt repeats, but
        each pass ends as plain ``pending`` (a counted retry with backoff and the
        retry cap), never as ``compile_progress``."""
        self.edit()
        blocked = self.compile_dir / refresh_settlement.SETTLED_NAME
        blocked.mkdir()
        results = [self.selected_pass() for _ in range(PASSES)]
        self.assertEqual([(r['state'], r.get('reason')) for r in results], [('pending', None)] * PASSES)
        self.assertEqual(len(self.calls), PASSES)
        self.assertTrue(blocked.is_dir())
        # Once the record can be written again one more attempt settles it.
        blocked.rmdir()
        self.assertEqual(self.selected_pass()['state'], 'drained')
        self.assertEqual(self.selected_pass()['state'], 'drained')
        self.assertEqual(len(self.calls), PASSES + 1)

    def test_a_damaged_settlement_file_is_read_as_empty_and_replaced_by_the_next_write(self):
        event = self.edit()
        self.selected_pass()
        path = self.compile_dir / refresh_settlement.SETTLED_NAME
        for damaged in ('{broken', '[1, 2]', ''):
            with self.subTest(damaged=damaged):
                before = len(self.calls)
                path.write_text(damaged, encoding='utf-8')
                # Nothing is read as settled: the source is attempted once more...
                self.assertEqual(self.selected_pass()['state'], 'drained')
                self.assertEqual(len(self.calls), before + 1)
                # ...and that attempt's write puts a well-formed record back.
                self.assertEqual(list(self.settled()), [event['eventId']])
                self.assertEqual(self.selected_pass()['state'], 'drained')
                self.assertEqual(len(self.calls), before + 1)
        self.assertEqual([name for name in sorted(p.name for p in self.compile_dir.iterdir())
                          if name.startswith('.write-')], [])


class Progress(FixtureCase):
    """``compile_progress`` means the backlog got smaller, whatever the worker counted."""

    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.queue = self.root / '.memvane/compile/source-queue.jsonl'
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.bound = ({'projectRoot': str(self.root), 'recallSettings': {'skipPaths': []}},
                      {'productRouteAllowed': True})

    def rows(self, count):
        self.queue.write_text(''.join(json.dumps({'n': n}) + '\n' for n in range(count)), encoding='utf-8')

    def result(self, before, after, report, *, claimed=0):
        self.rows(before)
        def child(argv, **_kwargs):
            self.rows(after)
            claim = self.queue.with_name(self.queue.name + '.claimed.1.x')
            claim.unlink(missing_ok=True)
            if claimed:
                claim.write_text('{}\n' * claimed, encoding='utf-8')
            return SimpleNamespace(returncode=0, stdout=json.dumps({'processed': 1, **report}), stderr='')
        with patch.object(execution, 'selected', return_value=self.bound), \
                patch.object(execution, 'recheck'), \
                patch.object(workspace, 'claim_lifecycle', return_value=None), \
                patch.object(execution.subprocess, 'run', side_effect=child), \
                patch.object(execution, '_compile_idle', return_value=False):
            return execution.run(self.root)

    def test_progress_is_a_smaller_backlog(self):
        cases = [(3, 2, {'completed': 1, 'staleDropped': 0}, 0, True),
                 (3, 2, {'completed': 0, 'staleDropped': 0}, 0, True),
                 (3, 2, {}, 0, True),
                 (5, 0, {'completed': 0, 'staleDropped': 5}, 0, True),
                 # Finished jobs that leave the backlog as it was are not progress.
                 (3, 3, {'completed': 3, 'staleDropped': 0}, 0, False),
                 (3, 4, {'completed': 1, 'staleDropped': 2}, 0, False),
                 # Rows a worker still holds in a claim are backlog too.
                 (3, 1, {'completed': 2}, 2, False),
                 (3, 1, {'completed': 2}, 1, True)]
        for before, after, report, claimed, productive in cases:
            with self.subTest(before=before, after=after, report=report, claimed=claimed):
                result = self.result(before, after, report, claimed=claimed)
                self.assertEqual(result['state'], 'pending')
                self.assertEqual(result.get('reason') == 'compile_progress', productive)

    def test_an_unsettled_refresh_event_counts_and_a_settled_one_does_not(self):
        self.project.write_note('docs/a.md', 'A.\n')
        self.rows(0)
        event = wiki_freshness.record_pending_refresh(self.root, 'docs/a.md', 'claude')
        self.assertEqual(execution._backlog(self.root, self.queue, self.bound), 1)
        sha = wiki_freshness.snapshot_file(self.root / 'docs/a.md')['sha256']
        self.assertTrue(refresh_settlement.settle(self.root, event, sha, 'not_refreshed'))
        self.assertEqual(execution._backlog(self.root, self.queue, self.bound), 0)
        self.assertTrue(execution._idle_locked(self.root, self.queue,
                                               self.queue.with_name('verify-queue.jsonl'), self.bound))
        # An unreadable ledger is unknown, not empty.
        (self.queue.parent / 'source-refresh-pending.jsonl').write_text('{broken\n', encoding='utf-8')
        self.assertIsNone(execution._backlog(self.root, self.queue, self.bound))


class Settlement(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        for name in ('a', 'b'):
            self.project.write_note('docs/%s.md' % name, name + '\n')
        self.path = self.root / '.memvane/compile' / refresh_settlement.SETTLED_NAME

    def event(self, name):
        return wiki_freshness.record_pending_refresh(self.root, 'docs/%s.md' % name, 'claude')

    def sha(self, name):
        return wiki_freshness.snapshot_file(self.root / ('docs/%s.md' % name))['sha256']

    def open_ids(self, events):
        return [item['eventId'] for item in refresh_settlement.unsettled(self.root, events)]

    def test_an_entry_holds_only_for_its_event_and_the_settled_content(self):
        a, b = self.event('a'), self.event('b')
        self.assertEqual(self.open_ids([a, b]), [a['eventId'], b['eventId']])
        self.assertFalse(self.path.exists())
        self.assertTrue(refresh_settlement.settle(self.root, a, self.sha('a'), 'not_refreshed'))
        self.assertEqual(self.open_ids([a, b]), [b['eventId']])
        (self.root / 'docs/a.md').write_text('other bytes\n', encoding='utf-8')
        self.assertEqual(self.open_ids([a, b]), [a['eventId'], b['eventId']])
        # A settlement recorded for a missing source holds while it stays missing.
        self.assertTrue(refresh_settlement.settle(self.root, b, None, 'source_unreadable'))
        self.assertEqual(self.open_ids([b]), [b['eventId']])
        (self.root / 'docs/b.md').unlink()
        self.assertEqual(self.open_ids([b]), [])

    def test_refused_and_unreadable_records_mean_not_settled(self):
        a = self.event('a')
        for bad in (None, {}, {'eventId': a['eventId']}, {'eventId': '', 'sourcePath': 'docs/a.md'}):
            self.assertFalse(refresh_settlement.settle(self.root, bad, self.sha('a'), 'x'))
        self.assertFalse(refresh_settlement.settle(self.root, a, 'not-a-sha', 'x'))
        # An event that is not the source's latest unresolved one is not recorded.
        stale = dict(a, eventId='0' * 32)
        self.assertFalse(refresh_settlement.settle(self.root, stale, self.sha('a'), 'x'))
        self.assertFalse(self.path.exists())
        self.assertTrue(refresh_settlement.settle(self.root, a, self.sha('a'), 'x' * 200))
        self.assertEqual(len(json.loads(self.path.read_text(encoding='utf-8'))[a['eventId']]['reason']), 80)
        for broken in ('{broken', '[]', json.dumps({a['eventId']: {'sourcePath': 3}}),
                       json.dumps({a['eventId']: {'sourcePath': 'docs/a.md', 'sourceSha256': 'zz'}})):
            self.path.write_text(broken, encoding='utf-8')
            self.assertEqual(self.open_ids([a]), [a['eventId']], broken)

    def test_a_write_keeps_only_events_that_are_still_unresolved(self):
        a, b = self.event('a'), self.event('b')
        self.assertTrue(refresh_settlement.settle(self.root, a, self.sha('a'), 'x'))
        self.assertTrue(refresh_settlement.settle(self.root, b, self.sha('b'), 'x'))
        self.assertEqual(sorted(json.loads(self.path.read_text(encoding='utf-8'))),
                         sorted([a['eventId'], b['eventId']]))
        newer = self.event('a')  # the source's latest event replaces the older one
        self.assertTrue(refresh_settlement.settle(self.root, newer, self.sha('a'), 'x'))
        self.assertEqual(sorted(json.loads(self.path.read_text(encoding='utf-8'))),
                         sorted([newer['eventId'], b['eventId']]))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
