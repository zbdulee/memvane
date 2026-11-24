"""Main-checkout recall over the journal-selected schema3 view.

Recall reads the published view only: no raw source file is opened, a branch
without a published head answers ``view_preparing`` without context (never the
main view's rows), a checkout that keeps moving answers ``checkout_changed``,
and a hot store journal is a recovery refusal that asks for the sync owner.
A lexical-first reader uses complete sets matching its current members. A
cold provider serves lexical rows with one owner wake.
"""
from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock

from memvane.journal.types import JournalEvidenceError
from memvane.recall import api as recall_api
from memvane.recall.types import HostResponseError
from memvane.recall.tests.support import NOTE, RecallInstallCase
from tests.support.case import scaled
from tests.support.project import settle_index
from tests.support.sqlite_hot_journal import kill_mid_write

HOSTS = ('claude', 'codex', 'hermes')
REPOSITORY = Path(__file__).resolve().parents[4]
PROMPT = 'what marker does the heron protocol keep?'
_OPENED = []


def _audit(event, args):
    if event == 'open' and _OPENED and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED[-1].append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


class CheckoutMoved(Exception):
    reason = 'checkout_changed'


class Schema3Recall(RecallInstallCase):
    CARDS = 2

    def test_semantic_sources_and_kernel_statistics_reach_all_host_events(self):
        self.install.provider.admit_queries()
        for switch in ('on', 'off'):
            with mock.patch.dict(os.environ, {'MEMVANE_VECTOR_ACCEL': switch}):
                kernel = 'accelerate' if sys.platform == 'darwin' and switch != 'off' else 'python'
                for host in HOSTS:
                    with self.subTest(switch=switch, host=host):
                        context, _err, event = self.recall(PROMPT, host)
                        self.assertIn('zqheronx', context)
                        self.assertEqual(event['semantic'], 'complete')
                        self.assertEqual(event['vectorKernel'], kernel)
                        self.assertGreater(event['vectorRows'], 0)
                        self.assertGreater(event['vectorVerified'], 0)
                        self.assertNotIn('semanticSources', event)
                        for key in ('vectorLoadMs', 'vectorScanMs', 'vectorVerifyMs'):
                            self.assertGreaterEqual(event[key], 0)

    def test_corrupt_vector_set_keeps_lexical_recall_and_requests_owner(self):
        self.install.provider.admit_queries()
        with closing(sqlite3.connect(self.install.state / 'store.sqlite')) as db, db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='vectors_update'").fetchone()[0]
            db.execute('DROP TRIGGER vectors_update')
            db.execute('UPDATE vectors SET embedding=? WHERE rowid=(SELECT min(rowid) FROM vectors)',
                       (struct.pack('<ff', 0.0, 1.0),))
            db.execute(trigger)
        context, err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual((event['outcome'], event['semantic'], event['vector_fallback']),
                         ('served', 'embedding_set_invalid', 'selected_embedding_set_invalid'))
        self.assertIn('selected_embedding_set_invalid', err)
        self.assertEqual([kind for kind, _target, _kw in self.wakes], ['laya-sync'])
        for _ in range(2):
            again, _err, event = self.recall(PROMPT)
            self.assertIn('zqheronx', again)
            self.assertEqual(event['vector_fallback'], 'selected_embedding_set_invalid')
        self.assertEqual([kind for kind, _target, _kw in self.wakes], ['laya-sync'])
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        decision = select_route(str(self.project), action='update')
        answer = serve_selected('update-queued', 'claude', decision,
                                {'cwd': str(self.project), 'hook_event_name': 'SessionStart'})
        notice = answer['hookSpecificOutput']['additionalContext']
        self.assertIn('임베딩 데이터가 손상돼 어휘 검색만 씁니다', notice)
        self.assertIn('restore → setup', notice)

    def test_nonfinite_semantic_rows_keep_lexical_recall(self):
        self.install.provider.admit_queries()
        with closing(sqlite3.connect(self.install.state / 'store.sqlite')) as db, db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='vectors_update'").fetchone()[0]
            db.execute('DROP TRIGGER vectors_update')
            db.execute('UPDATE vectors SET embedding=?', (struct.pack('<ff', float('nan'), 1.),))
            db.execute(trigger)
        context, _err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual(event['semantic'], 'embedding_set_invalid')
        self.assertEqual(event['outcome'], 'served')

    def test_semantic_set_validation_errors_keep_lexical_selector_and_reason(self):
        from memvane.store import api as store_api
        from memvane.store.types import BlobStoreError
        self.install.provider.admit_queries()
        real_open = store_api.open_store
        reasons = ('embedding_set_missing', 'embedding_input_mismatch',
                   'embedding_dimension_mismatch', 'embedding_sequence_invalid',
                   'vector_rows_mismatch')
        for reason in reasons:
            selected = []
            def open_store(*args, **kwargs):
                store = real_open(*args, **kwargs)
                def failed(*_args, **_kwargs):
                    raise BlobStoreError(reason)
                store.search_exact = failed
                return store
            def select(pool, **_kwargs):
                selected.extend(pool)
                return {'reason': 'selected', 'final_results': pool[:1]}
            with self.subTest(reason=reason), \
                    mock.patch.object(store_api, 'open_store', side_effect=open_store), \
                    mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                    mock.patch('memvane.learning.api.select_published', side_effect=select):
                context, _err, event = self.recall(PROMPT)
                self.assertGreater(len(selected), 0)
                self.assertIn('zqheronx', context)
                self.assertIn('local Laya model selected these cards', context)
                self.assertEqual(event['outcome'], 'served')
                self.assertEqual(event['semantic'], 'embedding_set_invalid')
                self.assertEqual(event['semanticReason'], reason)
                self.assertEqual(event['vector_fallback'], 'selected_embedding_set_invalid')

    def test_actual_input_sha_mismatch_preserves_lexical_in_both_vector_lanes(self):
        self.install.provider.admit_queries()
        with closing(sqlite3.connect(self.install.state / 'store.sqlite')) as db, db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='vectors_update'").fetchone()[0]
            db.execute('DROP TRIGGER vectors_update')
            db.execute('UPDATE vectors SET input_sha=?', ('0' * 64,))
            db.execute(trigger)
        for switch in ('on', 'off'):
            with self.subTest(switch=switch), \
                    mock.patch.dict(os.environ, {'MEMVANE_VECTOR_ACCEL': switch}):
                context, _err, event = self.recall(PROMPT)
                self.assertIn('zqheronx', context)
                self.assertEqual(event['outcome'], 'served')
                self.assertEqual(event['semantic'], 'embedding_set_invalid')
                self.assertEqual(event['semanticReason'], 'embedding_input_mismatch')
                expected = 'accelerate' if sys.platform == 'darwin' and switch == 'on' else 'python'
                self.assertEqual(event['vectorKernel'], expected)
        self.assertEqual([kind for kind, _target, _kw in self.wakes], ['laya-sync'])

    def test_semantic_store_deadlines_keep_lexical_candidates_and_selector(self):
        from contextlib import contextmanager
        from memvane.journal import api as journal
        from memvane.store.types import BlobStoreError
        self.install.provider.admit_queries()
        real_pin = journal.pin_schema3
        for reason in ('fts_deadline_exceeded', 'reader_snapshot_deadline'):
            error = (ValueError(reason) if reason == 'reader_snapshot_deadline'
                     else BlobStoreError(reason))
            @contextmanager
            def pinned(*args, **kwargs):
                with real_pin(*args, **kwargs) as (store, handle, pin):
                    with mock.patch.object(store, 'search_exact', side_effect=error):
                        yield store, handle, pin
            selected = []
            def select(pool, **_kwargs):
                selected.extend(pool)
                return {'reason': 'selected', 'final_results': pool[:1]}
            with mock.patch.object(journal, 'pin_schema3', side_effect=pinned), \
                    mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                    mock.patch('memvane.learning.api.select_published', side_effect=select):
                context, _err, event = self.recall(PROMPT)
            self.assertGreater(len(selected), 0)
            self.assertIn('zqheronx', context)
            self.assertEqual(event['semantic'], 'deferred_deadline')
            self.assertEqual(event['outcome'], 'served')

    def opened(self, call):
        _OPENED.append([])
        try:
            return call(), _OPENED[-1]
        finally:
            _OPENED.pop()

    def generation(self, context):
        header = context.split('\n', 1)[0]
        self.assertIn('generation ', header)
        return header.split('generation ', 1)[1].split(';', 1)[0]

    def test_main_recall_reads_the_published_view_for_every_host(self):
        contexts = {}
        for host in HOSTS:
            with self.subTest(host=host):
                (context, err, event), opened = self.opened(lambda: self.recall(PROMPT, host))
                contexts[host] = context
                self.assertIn('zqheronx', context)
                self.assertNotIn('zqglacierx', context)
                self.assertEqual([path for path in opened if path.endswith('notes.md')], [])
                self.assertEqual((event['outcome'], event['store_format']), ('served', 'schema3'))
                self.assertTrue(event['reader_endpoint'])
                self.assertEqual(event['selection_fallback'], 'laya_selector_not_prepared')
                self.assertEqual(event['vector_fallback'], 'selected_provider_resource_deferred')
                self.assertEqual(event['notes'], ['laya_vector_selected_provider_resource_deferred'])
                self.assertIn('selected_provider_resource_deferred; serving lexical results', err)
        self.assertEqual(len(set(contexts.values())), 1, 'every host renders the same context')
        self.assertEqual(self.generation(contexts['claude']), event['generation'])

    def test_refused_query_admission_serves_lexical_rows_with_one_owner_wake(self):
        results = []
        real = recall_api.query_endpoint

        def observed(*args, **kwargs):
            results.append(real(*args, **kwargs))
            return results[-1]
        with mock.patch('memvane.recall.api.query_endpoint', side_effect=observed):
            context, _err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual([result.semantic_status for result in results], ['provider_absent'])
        self.assertEqual(event['vector_fallback'], 'selected_provider_resource_deferred')
        self.assertEqual({(kind, target) for kind, target, _kwargs in self.wakes},
                         {('laya-sync', str(self.project))})

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
        # The owner publishes the real new member hashes without filling the changed card.
        with mock.patch('memvane.store.api.provider_spec', return_value=None):
            result = self.install.run_sync()
        self.assertEqual(result['vector']['state'], 'off', result)
        return result['generation']

    def test_unsealed_recall_scores_only_current_members_with_matching_sets(self):
        generation = self.publish_changed_lexical_head()
        self.forge_reader_without_vector_spec()
        self.install.provider.admit_queries()
        self.install.provider.bound_queries()
        for switch in ('on', 'store', 'off'):
            with self.subTest(switch=switch), mock.patch.dict(os.environ, {'MEMVANE_VECTOR_ACCEL': switch}):
                context, _err, event = self.recall('zqnoftsmatchx')
                self.assertIn('zqheronx', context)
                self.assertNotIn('zqneweditionx', context)
                self.assertEqual((event['generation'], event['semantic'], event['semanticOnlyInjected']),
                                 (generation, 'partial', 1))
                self.assertEqual((event['vectorCoveredMembers'], event['vectorEligibleMembers']), (1, 2))
                self.assertIsNone(event.get('vector_fallback'))
                context, _err, event = self.recall('zqneweditionx')
                self.assertIn('zqneweditionx', context)
                scores = {Path(row['path']).name: score for row, score in
                          zip(event['cards'], event['injectedCosine'])}
                self.assertIsNone(scores['glacier.md'])
                self.assertEqual(scores['heron.md'], 1.0)
        self.assertEqual(self.install.provider.query_lengths(), [len('zqnoftsmatchx'), len('zqneweditionx')] * 3)

    def test_partial_recall_skips_a_mismatched_chunk_digest_and_keeps_lexical(self):
        self.publish_changed_lexical_head()
        self.install.provider.admit_queries()
        with closing(sqlite3.connect(self.install.store_path)) as db, db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='chunk_sets_update'").fetchone()[0]
            db.execute('DROP TRIGGER chunk_sets_update')
            db.execute("UPDATE chunk_sets SET set_digest=?", ('0' * 64,))
            db.execute(trigger)
        context, _err, event = self.recall('zqheronx')
        self.assertIn('zqheronx', context)
        self.assertEqual((event['semantic'], event['vectorCoveredMembers'], event['vectorEligibleMembers']),
                         ('lexical_only', 0, 2))
        self.assertEqual(event['vector_fallback'], 'selected_view_vectors_pending')
        self.assertNotIn('injectedCosine', event)

    def observed_reads(self):
        reads = []
        real = recall_api.query_endpoint

        def observed(*args, **kwargs):
            reads.append((kwargs['query_vector'], real(*args, **kwargs)))
            return reads[-1][1]
        return reads, mock.patch('memvane.recall.api.query_endpoint', side_effect=observed)

    def test_an_admitted_provider_embeds_the_query_and_the_read_is_semantic(self):
        self.install.provider.admit_queries()
        reads, patch = self.observed_reads()
        with patch:
            context, err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual([(vector, result.semantic_status) for vector, result in reads],
                         [([1.0, 0.0], 'complete')])
        self.assertEqual((event['outcome'], event.get('vector_fallback'), event.get('notes')),
                         ('served', None, None))
        self.assertNotIn('serving lexical results', err)
        self.assertEqual(self.wakes, [])

    def test_large_semantic_deadline_preserves_lexical_recall(self):
        from memvane.store.types import BlobStoreError
        from memvane.store import api as store_api
        self.install.provider.admit_queries()
        sizes = [5] * 3408 + [4] * 343  # 3,751 cards, 18,412 synthetic chunks.
        self.assertEqual(len(sizes), 3751)
        observed = []
        def slow_exact(_pin, _vector, _limit, **kwargs):
            observed.append((sum(sizes), kwargs['deadline_seconds'], kwargs['max_float_work']))
            if kwargs['deadline_seconds'] <= 2.0:
                raise BlobStoreError('vector_deadline_exceeded')
            return []
        selected = []
        def select(pool, **_kwargs):
            selected.extend(pool)
            return {'reason': 'selected', 'final_results': pool[:1]}
        real_open = store_api.open_store
        def open_store(*args, **kwargs):
            store = real_open(*args, **kwargs)
            store.search_exact = slow_exact
            return store
        with mock.patch.object(store_api, 'open_store', side_effect=open_store), \
                mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                mock.patch('memvane.learning.api.select_published', side_effect=select):
            context, _err, event = self.recall(PROMPT)
        self.assertTrue(selected)
        self.assertTrue(any('zqheronx' in row['body'] for row in selected))
        self.assertIn('local Laya model selected these cards', context)
        self.assertIn('zqheronx', context)
        self.assertEqual(event['outcome'], 'served')
        self.assertEqual(event['semantic'], 'deferred_deadline')
        self.assertEqual(observed[0][0], 18412)
        self.assertEqual(observed[0][1], 2.0)
        self.assertEqual(observed[0][2], 6000000)

    def git_children(self, call):
        """The Git children ``call()`` started: the checkout observation's own (by their fixed arguments)."""
        import subprocess
        real, seen = subprocess.Popen, []

        def started(argv, *args, **kwargs):
            if isinstance(argv, list) and 'log.showSignature=false' in argv:
                seen.append(argv[7:])
            return real(argv, *args, **kwargs)
        with mock.patch('subprocess.Popen', side_effect=started):
            call()
        return seen

    def test_one_recall_starts_a_bounded_number_of_git_children(self):
        # A checkout observation is twelve Git children. Where the observation cache holds
        # (macOS on APFS) a recall of an unchanged checkout in a warm process starts none; without
        # the cache (Linux) every observation of the recall pays them again.
        self.install.provider.admit_queries()
        for _ in range(2):
            started = [command for command in self.git_children(lambda: self.recall(PROMPT))
                       if command != ['--exec-path']]
            observations, rest = divmod(len(started), 12)
            self.assertEqual(rest, 0, started)
            self.assertEqual(started.count(['--version']), observations)
            self.assertLessEqual(observations, 18, len(started))
            if sys.platform != 'darwin':
                self.assertGreaterEqual(observations, 1)

    @unittest.skipUnless(sys.platform == 'darwin', 'the observation cache needs macOS on APFS')
    def test_a_cold_thread_observes_the_checkout_once_and_a_warm_one_not_at_all(self):
        # The observation cache is per thread: a new thread is what a new hook process is.
        import threading
        self.install.provider.admit_queries()
        counts, errors = [], []

        def two_recalls():
            try:
                for _ in range(2):
                    counts.append(self.git_children(lambda: self.recall(PROMPT)))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        thread = threading.Thread(target=two_recalls)
        thread.start()
        thread.join(scaled(120))
        self.assertEqual((errors, len(counts)), ([], 2))
        cold, warm = counts
        # One observation (twelve children) and, behind the macOS shim, at most the one proof of its target.
        self.assertEqual(len([command for command in cold if command != ['--exec-path']]), 12, cold)
        self.assertLessEqual(cold.count(['--exec-path']), 1)
        self.assertEqual(warm, [])

    def test_recall_event_records_stage_times_without_prompt_or_body(self):
        self.install.provider.admit_queries()
        context, _err, event = self.recall(PROMPT)
        self.assertTrue(context)
        for field in ('routeProofMs', 'queryEmbeddingMs', 'readerSetupMs', 'checkoutMs', 'pinMs', 'lexicalMs',
                      'semanticMs', 'locationsMs', 'selectorMs', 'stillCurrentMs', 'renderMs'):
            self.assertIsInstance(event.get(field), (int, float), field)
            self.assertGreaterEqual(event[field], 0)
        for field in ('vectorReadMs', 'vectorUnpackMs', 'vectorDotMs'):
            self.assertIsInstance(event.get(field), (int, float), field)
            self.assertGreaterEqual(event[field], 0)
        # The route proof says where its time went, by fixed stage names only.
        stages = event['routeProofStages']
        self.assertEqual(list(stages), ['route', 'records', 'attestation', 'provider', 'recheck'])
        self.assertTrue(all(type(value) in (int, float) and value >= 0 for value in stages.values()), stages)
        self.assertNotIn(PROMPT, json.dumps(event))
        self.assertNotIn(NOTE, json.dumps(event))
        self.assertIn(event['pinVerified'], ('receipt', 'full'))
        _context, _err, repeated = self.recall(PROMPT)
        self.assertEqual(repeated['pinVerified'], 'receipt')

    def test_an_unexpected_failure_of_the_query_round_trip_serves_lexical_rows(self):
        # Whatever the round trip raised (a thread that could not start, a bug): the recall goes on.
        from memvane.provider import api as provider_api
        self.install.provider.admit_queries()
        reads, patch = self.observed_reads()
        with patch, mock.patch.object(provider_api, 'query_vectors',
                                      side_effect=RuntimeError("can't start new thread")) as asked:
            context, _err, event = self.recall(PROMPT)
        self.assertEqual(asked.call_count, 1)
        self.assertIn('zqheronx', context)
        self.assertEqual([(vector, result.semantic_status) for vector, result in reads], [(None, 'provider_absent')])
        self.assertEqual(event['outcome'], 'served')

    def test_a_cold_provider_serves_lexical_rows_and_wakes_the_owner_once(self):
        self.install.provider.admit_queries()
        self.install.provider.answer_queries('provider_unavailable')
        reads, patch = self.observed_reads()
        with patch:
            context, err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual([(vector, result.semantic_status) for vector, result in reads],
                         [(None, 'provider_absent')])
        self.assertEqual((event['outcome'], event['vector_fallback']),
                         ('served', 'selected_provider_preparing'))
        self.assertGreaterEqual(event['providerWakeMs'], 0)
        self.assertEqual(event['notes'], ['laya_vector_selected_provider_preparing'])
        self.assertIn('selected_provider_preparing; serving lexical results', err)
        self.assertEqual([(kind, target, kwargs.get('pull_due')) for kind, target, kwargs in self.wakes],
                         [('laya-sync', str(self.project), True)])

    def test_terminal_provider_serves_prompt_lexical_rows_and_records_the_reason(self):
        from memvane.journal.api import select_route, verify_selected_route
        self.publish_changed_lexical_head()
        self.install.provider.admit_queries()
        self.install.provider.answer_queries('provider_unavailable')
        decision = select_route(str(self.project), action='recall')
        manifest = verify_selected_route(decision)['providerManifest']
        state = Path(manifest['serviceSocketPath'] + '.resident.state')
        with os.fdopen(os.open(state, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as handle:
            json.dump(dict(fingerprint=manifest['embeddingFingerprint'], reason='attestation_invalid',
                           terminal=True, at=time.time(), attempts=3), handle)
        context, _err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual((event['outcome'], event['vector_fallback'], event['notes']),
                         ('served', 'selected_provider_start_failed', ['laya_vector_selected_provider_start_failed']))
        self.assertEqual(self.wakes, [])

    def test_the_learning_selector_receives_the_published_rows(self):
        seen = []

        def select(pool, **_kwargs):
            seen.append([row['path'] for row in pool])
            return {'reason': 'selected', 'final_results': pool[:1]}
        with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                mock.patch('memvane.learning.api.select_published', side_effect=select):
            context, _err, event = self.recall(PROMPT)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0] and all(path.endswith('.md') and 'concepts/' in path
                                        for path in seen[0]), seen)
        self.assertIn('local Laya model selected these cards', context)
        self.assertEqual(event['count'], 1)
        self.assertIsNone(event.get('selection_fallback'))

    def test_a_new_branch_without_a_published_view_answers_preparing_without_context(self):
        self.fixture.git('checkout', '-q', '-b', 'cold-side')
        settle_index(self.project)
        for host in HOSTS:
            with self.subTest(host=host):
                self.wakes.clear()
                context, err, event = self.recall(PROMPT, host)
                self.assertEqual(context, '')
                self.assertEqual((event['outcome'], event['reason']), ('empty', 'view_preparing'))
                self.assertIn('laya_view_preparing', event.get('notes') or [])
                self.assertNotIn('request refused', err)
                self.assertEqual({(kind, target) for kind, target, _kwargs in self.wakes},
                                 {('laya-sync', str(self.project))})

    def test_a_checkout_that_keeps_moving_answers_without_context(self):
        real = recall_api.query_endpoint
        for moves, served in ((1, True), (2, False)):
            with self.subTest(moves=moves):
                left = [moves]

                def moving(*args, **kwargs):
                    if left[0]:
                        left[0] -= 1
                        raise CheckoutMoved('checkout_changed')
                    return real(*args, **kwargs)
                with mock.patch('memvane.recall.api.query_endpoint', side_effect=moving):
                    context, err, event = self.recall(PROMPT)
                if served:
                    self.assertIn('zqheronx', context)
                else:
                    self.assertEqual(context, '')
                    self.assertEqual((event['outcome'], event['reason']), ('empty', 'checkout_changed'))
                    self.assertNotIn('request refused', err)

    def test_recalls_during_a_publish_only_serve_recorded_generations(self):
        old = self.generation(self.recall(PROMPT)[0])
        self.fixture.write_note('notes.md', NOTE + 'The heron also keeps zqheronx near the glacier.\n')
        self.write_card('heron')
        self.write_card('glacier')
        self.fixture.commit('second card')
        # The publish runs in its own process, as the sync worker does beside a hook's recall.
        out = self.tmp / 'publish.json'
        worker = subprocess.Popen(
            [sys.executable, '-B', '-m', 'memvane.recall.tests.publish_driver', str(self.project),
             str(self.install.journal), str(out)],
            cwd=str(REPOSITORY), env=dict(os.environ), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (worker.poll() is None and worker.terminate(), worker.wait(), worker.stderr.close()))
        seen = []
        try:
            while worker.poll() is None and len(seen) < 50:
                try:
                    context, _err, event = self.recall(PROMPT)
                except HostResponseError as exc:
                    # Two changes inside one recall (a retry already spent):
                    # refused, never served from a third or mixed view.
                    seen.append(str(exc))
                    continue
                except JournalEvidenceError as exc:
                    # A concurrently changing SQLite rollback journal also
                    # refuses proof before any rows can be served.
                    if str(exc) not in ('selected_route_changed', 'journal_changed'):
                        raise
                    seen.append(str(exc))
                    continue
                if context:
                    self.assertEqual(self.generation(context), event['generation'])
                seen.append(self.generation(context) if context else None)
        finally:
            code = worker.wait(timeout=scaled(120))
        report = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {'error': worker.stderr.read()}
        # A failure names the SQLite in use and the exception's args and errno.
        self.assertEqual((code, report.get('error')), (0, None), report)
        published = report['published']
        new = self.generation(self.recall(PROMPT)[0])
        self.assertNotEqual(new, old)
        self.assertIn(new, published)
        self.assertLessEqual(set(seen), {old, *published, 'selected_proof_changed',
                                         'selected_route_changed', 'journal_changed'}, (seen, published))

    def test_a_hot_store_journal_is_rolled_back_before_the_reader_serves(self):
        # A writer killed mid-commit leaves a torn store with a hot journal right
        # before the reader opens. The recall's own route proof recovers it
        # (as route classification does, tests/e2e): the uncommitted
        # transaction rolls back and the published card serves.
        from memvane.journal import api as journal_api
        store = self.install.store_path
        real = journal_api.resolve_endpoint
        torn = {}

        def resolve(*args, **kwargs):
            if not torn:
                _before, torn['journal'] = kill_mid_write(store)
            return real(*args, **kwargs)
        with mock.patch('memvane.journal.api.resolve_endpoint', side_effect=resolve):
            context = self.recall(PROMPT, session='hot')[0]
        self.assertIn('zqheronx', context)
        self.assertFalse(torn['journal'].exists())
        with self.install.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='hot_probe'")
                             .fetchone()[0], 0)

if __name__ == '__main__':
    unittest.main()
