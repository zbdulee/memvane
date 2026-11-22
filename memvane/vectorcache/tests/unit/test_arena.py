"""The vector arena format and its reader: what is believed, and what reads as "no arena"."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from memvane.vectorcache import arena

SPEC = dict(profile_id='p' * 8, recipe_id='r' * 8, dimension=2, store_incarnation='i' * 32)
NONCE = 'ab' * 16


def sha(index):
    return '%064x' % index


def rows(*values):
    return b''.join(struct.pack('<2f', *value) for value in values)


class ArenaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(os.path.realpath(self.tmp.name)) / arena.DIRECTORY
        self.directory.mkdir(mode=0o700)
        self.identity = arena.identity(nonce=NONCE, **SPEC)

    def write(self, name, data, mode=0o600):
        path = self.directory / name
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def build(self, sets, *, current=None, tail=b''):
        """An arena of ``[(blob sha, set digest, row bytes)]``; returns ``{blob: (first row, rows)}``."""
        vec, idx, spans, first = arena.vec_header(self.identity), arena.idx_header(self.identity), {}, 0
        for blob, digest, data in sets:
            count = len(data) // 8
            vec += data
            idx += arena.record(blob, first, count, digest, zlib.crc32(data))
            spans[blob] = (first, count)
            first += count
        vec_name, idx_name = arena.names(NONCE)
        self.write(vec_name, vec)
        self.write(idx_name, idx + tail)
        self.write(arena.CURRENT, json.dumps(self.identity if current is None else current).encode())
        return spans

    def open(self, **changes):
        found = arena.open_arena(self.directory, **{**SPEC, **changes})
        if found is not None:
            self.addCleanup(found.close)
        return found


class Format(ArenaCase):
    def test_records_round_trip_and_the_last_record_of_a_blob_wins(self):
        raw = (arena.record(sha(1), 0, 2, sha(11), 7) + arena.record(sha(2), 2, 1, sha(12), 8)
               + arena.record(sha(1), 3, 4, sha(13), 9))
        self.assertEqual(len(raw), 3 * arena.RECORD_BYTES)
        self.assertEqual(arena.records(raw), {sha(1): (3, 4, sha(13), 9), sha(2): (2, 1, sha(12), 8)})

    def test_a_torn_record_is_skipped_and_the_records_after_it_are_read(self):
        good = arena.record(sha(1), 0, 2, sha(11), 7)
        torn = bytearray(arena.record(sha(2), 2, 1, sha(12), 8))
        torn[5] ^= 1
        after = arena.record(sha(3), 3, 1, sha(13), 9)
        self.assertEqual(list(arena.records(good + bytes(torn) + after)), [sha(1), sha(3)])
        # What the writer leaves of a torn record: zeros up to the record boundary.
        padded = after[:40] + b'\0' * (arena.RECORD_BYTES - 40)
        self.assertEqual(list(arena.records(good + padded + after)), [sha(1), sha(3)])
        self.assertEqual(list(arena.records(good + after[:40])), [sha(1)])
        self.assertEqual(arena.records(b''), {})

    def test_touching_spans_are_merged_into_one_scan(self):
        self.assertEqual(arena.merge([(4, 2), (0, 2), (2, 2), (9, 1), (10, 3)]), [(0, 6), (9, 4)])
        self.assertEqual(arena.merge([]), [])

    def test_headers_carry_the_whole_identity(self):
        self.assertEqual(len(arena.vec_header(self.identity)), arena.PAGE)
        self.assertEqual(len(arena.idx_header(self.identity)), arena.IDX_HEADER)
        other = dict(self.identity, dimension=3)
        self.assertNotEqual(arena.vec_header(other), arena.vec_header(self.identity))
        self.assertNotEqual(arena.idx_header(other), arena.idx_header(self.identity))


class Reader(ArenaCase):
    def test_a_set_is_covered_only_when_its_record_digest_range_and_bytes_hold(self):
        data = {1: rows((1, 0), (0, 1)), 2: rows((3, 4)), 3: rows((0, 2)), 4: rows((5, 5))}
        self.build([(sha(n), sha(100 + n), data[n]) for n in data])
        found = self.open()
        self.assertEqual((found.rows, found.width, len(found.records)), (5, 8, 4))
        sets = {sha(1): (2, sha(101)), sha(2): (1, sha(102)), sha(3): (1, sha(999)),   # another digest
                sha(4): (2, sha(104)),                                                 # another chunk count
                sha(5): (1, sha(105))}                                                 # no record
        covered, missing, inconsistent = found.cover(sets)
        self.assertEqual(covered, {sha(1): (0, 2), sha(2): (2, 1)})
        self.assertEqual((sorted(missing), inconsistent), ([sha(3), sha(4), sha(5)], 2))
        self.assertEqual(bytes(found.span(0, 2)), data[1])
        self.assertEqual(bytes(found.span(2, 1)), data[2])

    def test_bytes_that_do_not_match_their_crc_or_lie_past_the_file_are_missing(self):
        spans = self.build([(sha(1), sha(101), rows((1, 0))), (sha(2), sha(102), rows((0, 1)))])
        vec = self.directory / arena.names(NONCE)[0]
        raw = bytearray(vec.read_bytes())
        raw[arena.PAGE + spans[sha(2)][0] * 8] ^= 0xff
        vec.write_bytes(bytes(raw))
        covered, missing, inconsistent = self.open().cover({sha(1): (1, sha(101)), sha(2): (1, sha(102))})
        self.assertEqual((list(covered), missing, inconsistent), ([sha(1)], [sha(2)], 1))
        # A vec file shorter than its index (a torn append): the set past its end is missing.
        vec.write_bytes(bytes(raw[:arena.PAGE + 8]))
        covered, missing, inconsistent = self.open().cover({sha(1): (1, sha(101)), sha(2): (1, sha(102))})
        self.assertEqual((list(covered), missing, inconsistent), ([sha(1)], [sha(2)], 1))

    def test_a_private_map_never_writes_the_file(self):
        self.build([(sha(1), sha(101), rows((1, 0)))])
        vec = self.directory / arena.names(NONCE)[0]
        before = vec.read_bytes()
        found = self.open()
        found.span(0, 1)[0] = 0x7f
        found.close()
        self.assertEqual(vec.read_bytes(), before)

    def test_an_empty_arena_covers_nothing(self):
        self.build([])
        found = self.open()
        self.assertEqual((found.rows, found.cover({sha(1): (1, sha(101))})), (0, ({}, [sha(1)], 0)))

    def test_another_identity_or_format_reads_as_no_arena(self):
        self.build([(sha(1), sha(101), rows((1, 0)))])
        self.assertIsNotNone(self.open())
        for change in ({'profile_id': 'q' * 8}, {'recipe_id': 'q' * 8}, {'dimension': 4},
                       {'store_incarnation': 'j' * 32}):
            with self.subTest(change=change):
                self.assertIsNone(self.open(**change))
        for current in (dict(self.identity, format=arena.FORMAT + 1), dict(self.identity, dtype='float16'),
                        dict(self.identity, nonce='zz'), {'nonce': NONCE}, ['x']):
            with self.subTest(current=str(current)[:40]):
                self.write(arena.CURRENT, json.dumps(current).encode())
                self.assertIsNone(self.open())
        # A key this reader does not know is not part of the identity.
        self.write(arena.CURRENT, json.dumps(dict(self.identity, lastUsed=1)).encode())
        self.assertIsNotNone(self.open())

    def test_missing_unsafe_or_damaged_files_read_as_no_arena(self):
        self.assertIsNone(arena.open_arena(self.directory / 'absent', **SPEC))
        self.assertIsNone(self.open())                      # an empty directory
        self.build([(sha(1), sha(101), rows((1, 0)))])
        vec_name, idx_name = arena.names(NONCE)
        for name in (arena.CURRENT, vec_name, idx_name):
            with self.subTest(open_to_group=name):
                os.chmod(self.directory / name, 0o640)
                self.assertIsNone(self.open())
                os.chmod(self.directory / name, 0o600)
        self.assertIsNotNone(self.open())
        with self.subTest('symlinked vec'):
            target = self.directory / 'elsewhere.vec'
            os.rename(self.directory / vec_name, target)
            os.symlink(target, self.directory / vec_name)
            self.assertIsNone(self.open())
            os.unlink(self.directory / vec_name)
            os.rename(target, self.directory / vec_name)
        with self.subTest('hard link'):
            os.link(self.directory / idx_name, self.directory / 'second.idx')
            self.assertIsNone(self.open())
            os.unlink(self.directory / 'second.idx')
        with self.subTest('header of another arena'):
            raw = (self.directory / vec_name).read_bytes()
            other = arena.vec_header(dict(self.identity, dimension=4))
            self.write(vec_name, other + raw[arena.PAGE:])
            self.assertIsNone(self.open())
            self.write(vec_name, raw)
        with self.subTest('idx header of another arena'):
            raw = (self.directory / idx_name).read_bytes()
            other = arena.idx_header(dict(self.identity, dimension=4))
            self.assertNotEqual(other, raw[:arena.IDX_HEADER])
            self.write(idx_name, other + raw[arena.IDX_HEADER:])
            self.assertIsNone(self.open())
            self.write(idx_name, raw)
            self.assertIsNotNone(self.open())
        with self.subTest('current is not json'):
            self.write(arena.CURRENT, b'{"nonce"')
            self.assertIsNone(self.open())
        os.chmod(self.directory, 0o750)
        self.assertIsNone(arena.open_arena(self.directory, **SPEC))

    def test_the_context_closes_the_arena_and_passes_no_directory_through(self):
        self.build([(sha(1), sha(101), rows((1, 0)))])
        with arena.opened(self.directory, **SPEC) as found:
            self.assertEqual(found.rows, 1)
        self.assertIsNone(found._mapped)
        with arena.opened(None, **SPEC) as found:
            self.assertIsNone(found)
