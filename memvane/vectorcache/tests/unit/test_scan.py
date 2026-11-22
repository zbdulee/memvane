"""Scanning covered sets from the arena: span grouping, per-set scores, refused bytes."""
import math
import struct
import unittest

from memvane.vectorcache import scan


class Refused(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Kernel:
    """A scan kernel over rows of two float32: cosines, refusing a zero row like the native one."""

    def __init__(self):
        self.calls = []

    def scores(self, buffer, dimension, query, deadline, stats):
        raw = bytes(buffer)
        self.calls.append(len(raw) // (dimension * 4))
        if deadline is not None and deadline < 0:
            raise Refused('vector_deadline_exceeded')
        answer = []
        for offset in range(0, len(raw), dimension * 4):
            values = struct.unpack_from('<%df' % dimension, raw, offset)
            norm = math.sqrt(sum(x * x for x in values))
            if not norm:
                raise Refused('embedding_values_invalid')
            answer.append(sum(a * b for a, b in zip(query, values)) / norm)
        return answer


class Arena:
    def __init__(self, rows):
        self.raw = bytearray(b''.join(struct.pack('<2f', *row) for row in rows))

    def span(self, first, rows):
        return memoryview(self.raw)[first * 8:(first + rows) * 8]


def run(arena, covered, queries=((1.0, 0.0),), deadline=None):
    kernel = Kernel()
    scores, unreadable = scan.scan(arena, covered, dimension=2, queries=queries, native=kernel,
                                   deadline=deadline, stats={})
    return scores, unreadable, kernel.calls


class Spans(unittest.TestCase):
    def test_sets_a_small_gap_apart_share_one_span(self):
        covered = {'a': (0, 2), 'b': (2, 3), 'c': (9, 1), 'd': (500, 4)}
        self.assertEqual(scan.spans(covered, gap=4), [(0, 10, ['a', 'b', 'c']), (500, 4, ['d'])])
        self.assertEqual(scan.spans(covered, gap=3), [(0, 5, ['a', 'b']), (9, 1, ['c']), (500, 4, ['d'])])
        self.assertEqual(scan.spans({}), [])
        # The default gap joins what a kernel call would cost more than scanning.
        self.assertEqual(len(scan.spans({'a': (0, 1), 'b': (scan.GAP_ROWS + 1, 1)})), 1)
        self.assertEqual(len(scan.spans({'a': (0, 1), 'b': (scan.GAP_ROWS + 2, 1)})), 2)


class Scan(unittest.TestCase):
    def test_each_set_gets_its_best_row_and_the_rows_between_are_dropped(self):
        arena = Arena([(1, 0), (0, 1), (9, 9), (3, 4), (0, 2)])   # row 2 belongs to no covered set
        scores, unreadable, calls = run(arena, {'a': (0, 2), 'b': (3, 2)})
        self.assertEqual((unreadable, calls), ([], [5]))
        self.assertAlmostEqual(scores['a'], 1.0)
        self.assertAlmostEqual(scores['b'], 0.6)

    def test_several_query_vectors_keep_each_sets_best(self):
        arena = Arena([(1, 0), (0, 1), (3, 4)])
        scores, _unreadable, calls = run(arena, {'a': (0, 1), 'b': (1, 1), 'c': (2, 1)},
                                         queries=((1.0, 0.0), (0.0, 1.0)))
        self.assertEqual(calls, [3, 3])
        self.assertEqual([round(scores[name], 4) for name in 'abc'], [1.0, 1.0, 0.8])

    def test_a_refused_row_between_sets_costs_a_rescan_and_no_set(self):
        arena = Arena([(1, 0), (0, 0), (0, 1)])
        scores, unreadable, calls = run(arena, {'a': (0, 1), 'b': (2, 1)})
        self.assertEqual((unreadable, calls), ([], [3, 1, 1]))
        self.assertEqual(sorted(scores), ['a', 'b'])

    def test_a_refused_row_inside_a_set_makes_only_that_set_unreadable(self):
        arena = Arena([(1, 0), (0, 0), (0, 1), (3, 4)])
        scores, unreadable, calls = run(arena, {'a': (0, 2), 'b': (2, 1), 'c': (3, 1)})
        self.assertEqual((unreadable, sorted(scores)), (['a'], ['b', 'c']))
        self.assertEqual(calls, [4, 2, 1, 1])

    def test_a_deadline_is_the_callers_to_see(self):
        arena = Arena([(1, 0), (0, 1)])
        with self.assertRaises(Refused) as caught:
            run(arena, {'a': (0, 1), 'b': (1, 1)}, deadline=-1)
        self.assertEqual(caught.exception.reason, 'vector_deadline_exceeded')
