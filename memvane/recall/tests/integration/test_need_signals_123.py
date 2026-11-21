"""1.0.23 F-3, F-4, F-5: the search-need probe is one count-only read with the lane's exclusions.

``RecallInstallCase`` with 20 cards and a ``probe_terms`` stand-in for the
signal arithmetic. Assertions are call counts, hit counts and scores.
"""
from __future__ import annotations

from contextlib import contextmanager
import time
from unittest import mock

from memvane.journal import api as journal_api
from memvane.recall import reader_dispatch
from memvane.recall.tests.support import RecallInstallCase
from memvane.recall.types import HYBRID_LIST_LIMIT
from memvane.settings.api import wiki_meta_pages


class _Store:
    """``probe_terms`` stand-in recording its arguments."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def probe_terms(self, pin, fts_queries, limit, *, excluded_paths=None, deadline_seconds=2):
        self.calls.append((list(fts_queries), limit, excluded_paths, deadline_seconds))
        if isinstance(self.answers, Exception):
            raise self.answers
        return list(self.answers)

    def search(self, *args, **kwargs):
        raise AssertionError('the probe never runs a full search')


class Signals(RecallInstallCase):
    CARDS = 1

    def signals(self, store, queries, *, remaining=30.0, excluded=None):
        timings = {}
        reader_dispatch._need_signals(store, object(), queries, timings, time.monotonic() + remaining,
                                      HYBRID_LIST_LIMIT, excluded=excluded)
        return timings

    def test_f4_rare_top_score_is_the_best_bm25_and_counts_are_per_atom(self):
        store = _Store([(2, -3.5), (HYBRID_LIST_LIMIT, -9.0), (0, None), (3, -1.25)])
        got = self.signals(store, ['a AND b', 'c AND d AND a'], excluded=['log.md'])
        self.assertEqual((got['needAtoms'], got['needAtomsHit'], got['needAtomsRare']), (4, 3, 2))
        # bm25: lower is better. A full list (``limit`` hits) is not rare whatever its score.
        self.assertEqual(got['needRareTopScore'], -3.5)
        self.assertFalse(got['needProbeTruncated'])
        self.assertEqual([(call[0], call[1], call[2]) for call in store.calls],
                         [(['a', 'b', 'c', 'd'], HYBRID_LIST_LIMIT, ['log.md'])])
        self.assertLessEqual(store.calls[0][3], reader_dispatch.NEED_BUDGET_SECONDS)

    def test_f3_unfinished_atoms_and_failures_are_truncated(self):
        got = self.signals(_Store([(1, -2.0)]), ['a AND b AND c'])
        self.assertEqual((got['needAtoms'], got['needAtomsHit'], got['needProbeTruncated']), (1, 1, True))
        got = self.signals(_Store(RuntimeError('probe boom')), ['a AND b'])
        self.assertEqual((got['needAtoms'], got['needProbeTruncated']), (0, True))
        self.assertIsNone(got['needRareTopScore'])

    def test_f8_probe_leaves_the_reader_its_reserve(self):
        reserve = reader_dispatch.NEED_READER_RESERVE_SECONDS
        # Under the reserve plus nothing: no read at all.
        store = _Store([(1, -1.0)])
        got = self.signals(store, ['a'], remaining=reserve - .5)
        self.assertEqual((store.calls, got['needAtoms'], got['needProbeTruncated']), ([], 0, True))
        # Just above it: the read gets only what is above the reserve, never the whole budget.
        store = _Store([(1, -1.0)])
        self.signals(store, ['a'], remaining=reserve + .05)
        self.assertEqual(len(store.calls), 1)
        self.assertLessEqual(store.calls[0][3], .05)
        self.assertGreater(store.calls[0][3], 0)

    def test_f4_lexical_top_score_is_the_best_bm25(self):
        timings = {}
        lanes = [[{'path': 'w/a.md', 'score': -4.0}, {'path': 'w/b.md', 'score': -1.0}],
                 [{'path': 'w/a.md', 'score': -2.5}]]
        reader_dispatch._lexical_signals(timings, ['q1', 'q2'], lanes)
        self.assertEqual((timings['lexicalQueries'], timings['lexicalHits'], timings['lexicalTopScore']),
                         (2, 2, -4.0))


class CountOnlyRead(RecallInstallCase):
    CARDS = 20

    @contextmanager
    def spied(self, seen, extra=None):
        real_pin = journal_api.pin_schema3

        @contextmanager
        def pinned(*args, **kwargs):
            with real_pin(*args, **kwargs) as (store, handle, pin):
                probe = store.probe_terms

                def probed(p, fts_queries, limit, **kw):
                    with mock.patch.object(store, '_blob', wraps=store._blob) as blob, \
                            mock.patch.object(store, '_observe', wraps=store._observe) as observe, \
                            mock.patch.object(store, '_reading', wraps=store._reading) as reading:
                        answer = probe(p, fts_queries, limit, **kw)
                    seen.append({'queries': list(fts_queries), 'answer': answer,
                                 'full': [store.search(p, query, limit) for query in fts_queries],
                                 'excluded': kw.get('excluded_paths'), 'blob': blob.call_count,
                                 'observe': observe.call_count, 'reading': reading.call_count,
                                 'extra': extra(probe, store, p, fts_queries, limit) if extra else None})
                    return answer
                with mock.patch.object(store, 'probe_terms', side_effect=probed):
                    yield store, handle, pin
        with mock.patch.object(journal_api, 'pin_schema3', side_effect=pinned), \
                mock.patch.object(reader_dispatch, 'NEED_BUDGET_SECONDS', 20.0):
            yield

    def test_f3_one_count_only_read_matches_the_full_search(self):
        seen = []
        with self.spied(seen):
            ctx, _err, event = self.recall('zqheronx zqsharedx zqabsentx')
        self.assertIn('zqheronx', ctx)
        self.assertEqual(len(seen), 1)
        probe = seen[0]
        self.assertEqual(len(probe['queries']), 3)
        # One read session, no card body and no checkout observation.
        self.assertEqual((probe['reading'], probe['blob'], probe['observe']), (1, 0, 0))
        # F-5: the reader passes the wiki-root meta pages its lexical lane drops.
        self.assertEqual(probe['excluded'], wiki_meta_pages())
        self.assertEqual(sorted(probe['excluded']), ['SCHEMA.md', 'index.md', 'log.md'])
        # The same count and best (lowest) score as the full search of each atom.
        for answer, full in zip(probe['answer'], probe['full']):
            self.assertEqual(answer, (len(full), min((row['score'] for row in full), default=None)))
        self.assertEqual(sorted(count for count, _ in probe['answer']), [0, 1, HYBRID_LIST_LIMIT])
        self.assertEqual((event['needAtoms'], event['needAtomsHit'], event['needAtomsRare']), (3, 2, 1))
        rare = next(score for count, score in probe['answer'] if count == 1)
        # The event log keeps three decimals.
        self.assertAlmostEqual(event['needRareTopScore'], rare, places=2)
        self.assertFalse(event['needProbeTruncated'])

    def test_f5_an_excluded_path_is_not_counted(self):
        def extra(probe, store, pin, queries, limit):
            return (probe(pin, queries, limit, excluded_paths=['concepts/heron.md']),
                    probe(pin, queries, limit, excluded_paths=['concepts/glacier.md']))
        seen = []
        with self.spied(seen, extra):
            self.recall('zqheronx zqsharedx')
        probe = seen[0]
        heron = next(index for index, query in enumerate(probe['queries']) if 'zqheronx' in query)
        shared = 1 - heron
        without_heron, without_glacier = probe['extra']
        self.assertEqual([row['path'] for row in probe['full'][heron]], ['concepts/heron.md'])
        self.assertEqual(without_heron[heron], (0, None))
        self.assertEqual(without_heron[shared][0], HYBRID_LIST_LIMIT - 1)
        self.assertEqual(without_glacier[heron][0], 1)
        self.assertEqual(without_glacier[shared][0], HYBRID_LIST_LIMIT - 1)

    def test_f3_a_spent_deadline_returns_the_finished_queries(self):
        def extra(probe, store, pin, queries, limit):
            spent = probe(pin, queries, limit, deadline_seconds=1e-9)
            # The pin still answers a count read and a full search afterwards.
            return spent, probe(pin, queries, limit), len(store.search(pin, queries[0], limit))
        seen = []
        with self.spied(seen, extra):
            _ctx, _err, event = self.recall('zqheronx zqsharedx')
        spent, later, full = seen[0]['extra']
        self.assertEqual(spent, [])
        self.assertEqual(later, seen[0]['answer'])
        self.assertEqual(full, seen[0]['answer'][0][0])
