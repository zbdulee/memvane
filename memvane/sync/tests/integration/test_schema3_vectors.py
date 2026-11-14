"""Vector generations of the selected view: fill after the lexical publish, resume, yield.

``SyncCase`` with the fixture provider (one fixed vector per chunk). Provider
calls are counted on the reviewed provider handle; a fill call stopped by its
deadline is injected on the opened store (``store.api.open_store`` wrapper).
Every check counts calls, sets and generations; none reads a wall clock.
"""
import unittest
from unittest import mock

from memvane.store.types import BlobStoreError
from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import Coordinator, SyncCase


def stopped_after_one(real, *args, **kwargs):
    """A fill call that embeds one target, then hits its deadline."""
    real(*args, **dict(kwargs, max_files=1))
    raise BlobStoreError('fill_deadline_exceeded')


def stopped_at_once(real, *args, **kwargs):
    """A fill call that hits its deadline before it embeds anything."""
    raise BlobStoreError('fill_deadline_exceeded')


class SyncVectors(SyncCase):
    def reader_spec(self):
        from memvane.journal.api import inspect_journal
        reader = inspect_journal(self.target.journal)['reader']
        return reader['vector_profile'], reader['vector_recipe']

    def wakes(self):
        wakes = []
        return wakes, mock.patch('memvane.runtime.api.request',
                                 lambda kind, target='', **kw: wakes.append((kind, target, kw.get('delay', 0)))
                                 or {'status': 'queued'})

    def edit(self, *tokens):
        for index, token in enumerate(tokens):
            self.card(self.CARDS[index][0], token)

    # -- the settled install --------------------------------------------------------
    def test_settled_install_seals_its_vector_spec_and_an_idle_pass_asks_no_provider(self):
        head = self.head()
        spec, coverage = self.coverage(head[0])
        self.assertEqual(coverage, 'complete')
        # The owner re-sealed the reader with the head's spec after the first vector generation.
        self.assertEqual(self.reader_spec(), (spec['profile_id'], spec['recipe_id']))
        epoch = self.epoch()
        with self.provider_calls() as calls:
            idle = self.run_pass()
        self.assertEqual((idle['state'], idle['vector'], idle.get('readerResettle')),
                         ('unchanged', {'state': 'current'}, None), idle)
        self.assertEqual((calls, self.head(), self.epoch()), ([], head, epoch))

    # -- refusals ------------------------------------------------------------------
    def test_resource_refusal_keeps_the_lexical_head_then_the_head_fill_seals_vectors(self):
        start = self.head()
        sealed = self.reader_spec()
        self.edit('falconprobe')
        with self.provider_calls() as calls:
            refused = self.run_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual((refused['state'], refused['reason'], refused['vector'], refused['ingested']),
                         ('deferred', 'cpu_pressure', {'state': 'deferred', 'reason': 'cpu_pressure'}, 1),
                         refused)
        self.assertEqual(refused['detail'], 'vec_deferred_cpu_pressure')
        self.assertEqual(calls, [])
        lexical = self.head()
        self.assertEqual(lexical, (refused['generation'], start[1] + 1))
        self.assertEqual(self.coverage(lexical[0]), (None, 'lexical'))
        # A lexical head under the vector-sealed reader still answers.
        self.assertEqual(self.reader_spec(), sealed)
        self.assertIn('concepts/alder-grove.md', self.recall('falconprobe'))
        # The next pass has nothing to ingest: it fills the head and seals the same members.
        with self.provider_calls() as calls:
            filled = self.run_pass()
        self.assertEqual((filled['state'], filled['ingested'], filled['vector']['state']),
                         ('indexed', 0, 'published'), filled)
        self.assertEqual(self.head(), (filled['generation'], lexical[1] + 1))
        self.assertEqual(self.members(filled['generation']), self.members(lexical[0]))
        self.assertEqual(self.coverage(filled['generation'])[1], 'complete')
        # Only the edited card's content lacked a set.
        self.assertEqual([kind for kind, _path in calls], ['chunks'])

    def test_provider_defect_ends_the_wake_blocked_and_a_refusal_stays_deferred(self):
        from memvane.journal.types import JournalEvidenceError
        start = self.head()
        self.edit('finchprobe')

        def invalid(_decision, _proof):
            raise JournalEvidenceError('worker_attestation_invalid')
        with mock.patch.object(worker, '_schema3_worker_manifest', invalid), self.provider_calls() as calls:
            blocked = self.run_pass()
        self.assertEqual((blocked['state'], blocked['reason'], blocked['vector']['state']),
                         ('blocked', 'vector_provider_worker_attestation_invalid', 'provider_blocked'), blocked)
        self.assertEqual(calls, [])
        self.assertEqual(self.head(), (blocked['generation'], start[1] + 1))
        self.assertEqual(self.coverage(blocked['generation'])[1], 'lexical')
        self.assertIn('concepts/alder-grove.md', self.recall('finchprobe'))
        # A refused resource is a retried deferral, never blocked.
        self.edit('sparrowprobe')
        deferred = self.run_pass(Coordinator(refuse='memory_reserve'))
        self.assertEqual((deferred['state'], deferred['reason']), ('deferred', 'memory_reserve'), deferred)

    def test_edit_back_to_stored_bytes_publishes_one_vector_generation_without_the_provider(self):
        original = (self.wiki / self.CARDS[0][0]).read_bytes()
        self.edit('falconprobe')
        edited = self.run_pass()
        self.assertEqual(edited['vector']['state'], 'published', edited)
        before = self.head()
        (self.wiki / self.CARDS[0][0]).write_bytes(original)
        with self.provider_calls() as calls:
            back = self.run_pass()
        self.assertEqual((back['state'], back['ingested'], back['vector']['state']),
                         ('indexed', 1, 'published'), back)
        self.assertEqual((back['vector']['filled'], calls), (0, []), back)
        self.assertEqual(self.head(), (back['generation'], before[1] + 1))
        self.assertEqual(self.coverage(back['generation'])[1], 'complete')

    # -- fill calls stopped by their deadline ----------------------------------------
    def test_progressing_pass_acks_and_reoffers_while_a_zero_progress_pass_keeps_its_wake(self):
        self.edit('falconprobe', 'hawkprobe')
        wakes, recorded = self.wakes()
        with recorded, self.store_methods(fill_view=stopped_at_once):
            stuck = self.run_pass()
        self.assertEqual((stuck['state'], stuck['reason'], stuck['vector']['progress']),
                         ('pending', 'schema3_vector_deadline', 0), stuck)
        self.assertNotIn('vectorReoffer', stuck)
        self.assertNotIn('progressKind', stuck)
        self.assertEqual(wakes, [])
        # A changed pass whose fill adds sets before its deadline: ACK and the same wake 30 s later.
        self.edit('falconprobe2', 'hawkprobe2', 'owlprobe2')
        with recorded, self.store_methods(fill_view=stopped_after_one):
            moved = self.run_pass()
        self.assertEqual((moved['state'], moved['vector']['state'], moved['vector']['progress'],
                          moved.get('vectorReoffer')), ('indexed', 'pending', 1, True), moved)
        self.assertEqual(wakes, [('laya-sync', str(self.project), worker.SCHEMA3_VECTOR_REOFFER_SECONDS)])
        self.assertTrue(moved['detail'].startswith('vec_pending_deadline:rem'), moved)
        with recorded:
            done = self.run_pass()
        self.assertEqual(done['vector']['state'], 'published', done)

    def test_vector_window_goes_on_after_a_call_that_added_sets_and_ends_at_one_that_added_none(self):
        self.edit('falconprobe', 'hawkprobe')
        refused = self.run_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual(refused['vector']['state'], 'deferred', refused)
        calls = []

        def counted(real, *args, **kwargs):
            calls.append(kwargs['max_files'])
            if len(calls) == 1:
                return stopped_after_one(real, *args, **kwargs)
            return real(*args, **kwargs)
        # Nothing to ingest: the unchanged head fills in the vector window.
        with self.store_methods(fill_view=counted):
            filled = self.run_pass()
        self.assertEqual((filled['state'], filled['ingested'], filled['vector']['state']),
                         ('indexed', 0, 'published'), filled)
        self.assertEqual(len(calls), 2)
        self.edit('falconprobe2')
        self.run_pass(Coordinator(refuse='cpu_pressure'))
        stops = []
        with self.store_methods(fill_view=lambda real, *a, **k: stops.append(1) or stopped_at_once(real, *a, **k)):
            ended = self.run_pass()
        self.assertEqual((ended['state'], ended['vector']['state'], ended['vector']['reason']),
                         ('pending', 'pending', 'schema3_vector_deadline'), ended)
        self.assertEqual(len(stops), 1)
        # The card over one fill call completes on a later pass.
        later = self.run_pass()
        self.assertEqual((later['state'], later['vector']['state']), ('indexed', 'published'), later)

    def test_a_cold_target_gets_the_resume_budget(self):
        self.card('concepts/owl-roost.md', 'owlprobe')
        seen = []
        with self.store_methods(fill_view=lambda real, *a, **k: seen.append(k['deadline_seconds']) or real(*a, **k)):
            result = self.run_pass()
        self.assertEqual(result['vector']['state'], 'published', result)
        self.assertEqual(seen[0], worker.SCHEMA3_RESUME_SECONDS)

    # -- initial build priority ------------------------------------------------------
    def test_vector_fill_yields_to_a_waiting_initial_build_before_a_provider_call(self):
        start = self.head()
        self.edit('falconprobe')
        with mock.patch.object(worker, '_initial_build_waiting', return_value=True), \
                self.provider_calls() as calls:
            yielded = self.run_pass()
            self.assertEqual((yielded['state'], yielded['reason'], yielded['vector']),
                             ('deferred', 'laya_sync_yield_priority',
                              {'state': 'deferred', 'reason': 'laya_sync_yield_priority'}), yielded)
            self.assertEqual(self.head(), (yielded['generation'], start[1] + 1))
            # The yield holds inside the vector window of an unchanged pass too.
            window = self.run_pass()
            self.assertEqual((window['state'], window['reason']), ('deferred', 'laya_sync_yield_priority'), window)
        self.assertEqual(calls, [])

    def test_a_yield_after_progress_reoffers_and_the_next_pass_repeats_no_embedding(self):
        self.edit('falconprobe', 'hawkprobe', 'owlprobe')
        wakes, recorded = self.wakes()
        answers = iter([False])
        with recorded, self.provider_calls() as calls, \
                mock.patch.object(worker, '_initial_build_waiting',
                                  side_effect=lambda *_a: next(answers, True)):
            moved = self.run_pass()
        self.assertEqual((moved['vector']['state'], moved['vector']['reason'], moved['vector']['progress'],
                          moved.get('vectorReoffer')), ('pending', 'laya_sync_yield_priority', 1, True), moved)
        self.assertEqual(len(calls), 1)
        self.assertEqual(wakes, [('laya-sync', str(self.project), worker.SCHEMA3_VECTOR_REOFFER_SECONDS)])
        with recorded, self.provider_calls() as calls:
            done = self.run_pass()
        self.assertEqual(done['vector']['state'], 'published', done)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
