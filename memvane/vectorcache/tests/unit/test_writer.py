"""The arena's writer: self-check, catch-up bounds, compaction, torn tails, format numbers and quota."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from memvane.vectorcache import arena, types, writer

SPEC = dict(profile_id='p' * 8, recipe_id='r' * 8, dimension=2, store_incarnation='i' * 32)
OLD = 30 * 24 * 3600


def sha(index):
    return '%064x' % index


def rows(count, seed):
    return b''.join(struct.pack('<2f', 1.0, seed + index + 1.0) for index in range(count))


class Store:
    """The store's side of the writer: the live sets and their bytes."""

    def __init__(self):
        self.sets, self.unreadable, self.reads = {}, set(), []

    def put(self, index, count=1, seed=None):
        self.sets[sha(index)] = (count, sha(1000 + index + (seed or 0)), rows(count, index + (seed or 0)))

    @property
    def live(self):
        return {blob: (count, digest) for blob, (count, digest, _data) in self.sets.items()}

    def read(self, blob):
        self.reads.append(blob)
        return None if blob in self.unreadable else self.sets[blob][2]


class WriterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.installs = Path(os.path.realpath(self.tmp.name)) / 'installs'
        self.root = self.installs / 'own'
        self.root.mkdir(parents=True, mode=0o700)
        self.directory = self.root / arena.DIRECTORY
        self.store = Store()

    def run_pass(self, *, add=False, **spec):
        """One writer pass (``start``, and ``add`` when asked); its summary."""
        made = writer.Writer(self.directory, **{**SPEC, **spec})
        try:
            made.start(self.store.live, self.store.read)
            if add:
                made.add(self.store.live, self.store.read)
            return made.summary()
        finally:
            made.close()

    def cover(self, **spec):
        """``(covered, missing, inconsistent)`` a recall's reader sees for the store's live sets."""
        with arena.opened(self.directory, **{**SPEC, **spec}) as found:
            if found is None:
                return None
            return found.cover(self.store.live)

    def current(self):
        return json.loads((self.directory / arena.CURRENT).read_text())

    def pair(self):
        return [self.directory / name for name in arena.names(self.current()['nonce'])]


class CatchUp(WriterCase):
    def test_a_new_arena_is_created_private_and_covers_every_live_set(self):
        for index in range(5):
            self.store.put(index, count=index + 1)
        report = self.run_pass()
        self.assertEqual((report['catchupRows'], report['remaining'], report['skipped'], report['rebuilt']),
                         (15, 0, None, False))
        covered, missing, wrong = self.cover()
        self.assertEqual((len(covered), missing, wrong), (5, [], 0))
        self.assertEqual(os.stat(self.directory).st_mode & 0o777, 0o700)
        self.assertEqual({os.stat(path).st_mode & 0o777 for path in self.directory.iterdir()}, {0o600})
        self.assertEqual(report['bytes'], 4096 + 15 * 8)
        # Nothing to do: nothing is read from the store and nothing is appended.
        self.store.reads.clear()
        again = self.run_pass()
        self.assertEqual((again['catchupRows'], again['repaired'], again['remaining'], self.store.reads), (0, 0, 0, []))

    def test_one_pass_appends_a_bounded_number_of_rows_and_the_next_goes_on(self):
        for index in range(10):
            self.store.put(index, count=2)
        with mock.patch.object(types, 'PASS_VECTORS', 6):
            first = self.run_pass()
            self.assertEqual((first['catchupRows'], first['remaining'], first['skipped']), (6, 7, None))
            self.assertEqual(len(self.cover()[0]), 3)
            second = self.run_pass(add=True)
            # The catch-up's bound and the bound of the appends ahead of a publication are separate.
            self.assertEqual((second['catchupRows'], second['appendRows'], second['remaining']), (6, 6, 1))
            third = self.run_pass()
        self.assertEqual((third['catchupRows'], third['remaining']), (2, 0))

    def test_a_set_larger_than_the_bound_is_still_appended_alone(self):
        self.store.put(1, count=9)
        self.store.put(2, count=1)
        with mock.patch.object(types, 'PASS_VECTORS', 4):
            report = self.run_pass()
        self.assertEqual((report['catchupRows'], report['remaining']), (9, 1))

    def test_an_expired_pass_clock_stops_after_the_first_set(self):
        for index in range(3):
            self.store.put(index)
        ticks = iter(range(0, 1000, 10))
        made = writer.Writer(self.directory, clock=lambda: next(ticks), **SPEC)
        self.addCleanup(made.close)
        made.start(self.store.live, self.store.read)
        self.assertEqual((made.summary()['catchupRows'], made.summary()['remaining']), (1, 2))

    def test_a_hundred_thousand_rows_are_covered_in_the_expected_number_of_passes(self):
        for index in range(12500):
            self.store.put(index, count=8)
        passes = []
        with mock.patch.object(types, 'PASS_SECONDS', 3600):
            for _ in range(40):
                passes.append(self.run_pass())
                if not passes[-1]['remaining']:
                    break
        self.assertEqual(len(passes), 25)  # ceil(100,000 / 4,096)
        self.assertEqual(sum(item['catchupRows'] for item in passes), 100000)
        self.assertTrue(all(item['catchupRows'] == 4096 for item in passes[:-1]))
        covered, missing, wrong = self.cover()
        self.assertEqual((len(covered), missing, wrong), (12500, [], 0))


class SelfCheck(WriterCase):
    def filled(self, count=4):
        for index in range(count):
            self.store.put(index, count=2)
        self.run_pass()
        return self.pair()

    def test_a_damaged_set_is_repaired_by_the_next_pass_and_is_not_progress(self):
        vec, _idx = self.filled()
        raw = bytearray(vec.read_bytes())
        raw[4096 + 2 * 8] ^= 0xFF  # The first row of the second set.
        vec.write_bytes(bytes(raw))
        covered, missing, wrong = self.cover()
        self.assertEqual((len(covered), len(missing), wrong), (3, 1, 1))
        report = self.run_pass()
        # The damaged rows do not stay between the sets: the pair is replaced in the same pass.
        self.assertEqual((report['repaired'], report['catchupRows'], report['remaining'], report['deadRows'],
                          report['compacted']), (1, 0, 0, 0, True))
        self.assertEqual(self.store.reads[-1:], missing)
        covered, missing, wrong = self.cover()
        self.assertEqual((len(covered), missing, wrong), (4, [], 0))
        self.assertFalse(vec.exists())
        self.assertEqual(self.pair()[0].stat().st_size, 4096 + 4 * 2 * 8)
        self.assertFalse(self.run_pass()['compacted'])

    def test_rows_zeroed_under_their_record_cost_a_recall_one_scan_after_the_repair(self):
        from memvane.vectorcache import scan
        vec, _idx = self.filled(count=50)
        raw = bytearray(vec.read_bytes())
        raw[4096 + 40 * 8:4096 + 42 * 8] = b'\0' * 16  # One set's rows, as a power cut leaves them.
        vec.write_bytes(bytes(raw))

        class Kernel:
            calls = 0

            def scores(self, buffer, dimension, query, deadline, stats):
                Kernel.calls += 1
                values = bytes(buffer)
                if any(values[at:at + 8] == b'\0' * 8 for at in range(0, len(values), 8)):
                    error = ValueError('embedding_values_invalid')
                    error.reason = 'embedding_values_invalid'
                    raise error
                return [1.0] * (len(values) // 8)

        def scanned():
            Kernel.calls = 0
            with arena.opened(self.directory, **SPEC) as found:
                covered = found.cover(self.store.live)[0]
                scores, unreadable = scan.scan(found, covered, dimension=2, queries=[[1.0, 0.0]], native=Kernel(),
                                               deadline=None, stats={})
            return Kernel.calls, len(scores), unreadable
        self.assertEqual(scanned(), (50, 49, []))  # The whole span is refused, then every set on its own.
        self.assertEqual(self.run_pass()['repaired'], 1)
        self.assertEqual(scanned(), (1, 50, []))

    def test_a_set_whose_store_digest_changed_is_appended_again(self):
        self.filled()
        self.store.put(1, count=3, seed=50)
        report = self.run_pass()
        self.assertEqual((report['repaired'], report['catchupRows'], report['remaining']), (1, 0, 0))
        self.assertEqual(len(self.cover()[0]), 4)

    def test_compaction_drops_dead_rows_and_does_not_carry_a_damaged_set_over(self):
        vec, _idx = self.filled(count=6)
        old = self.current()['nonce']
        # Four of six sets leave the views: more than half the rows are dead.
        for index in range(2, 6):
            del self.store.sets[sha(index)]
        raw = bytearray(vec.read_bytes())
        raw[4096] ^= 0xFF  # Set 0 is damaged on disk; set 1 holds.
        vec.write_bytes(bytes(raw))
        report = self.run_pass()
        self.assertTrue(report['compacted'])
        self.assertNotEqual(self.current()['nonce'], old)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         sorted([arena.CURRENT, *arena.names(self.current()['nonce'])]))
        # The damaged set came from the store, not from the old arena.
        self.assertEqual((report['repaired'], report['deadRows'], report['remaining']), (1, 0, 0))
        with arena.opened(self.directory, **SPEC) as found:
            covered, missing, wrong = found.cover(self.store.live)
            self.assertEqual((len(covered), missing, wrong), (2, [], 0))
            for blob, (first, count) in covered.items():
                self.assertEqual(bytes(found.span(first, count)), self.store.sets[blob][2])

    def test_a_set_whose_bytes_change_between_the_check_and_the_copy_is_not_carried_over(self):
        self.filled(count=6)
        for index in range(2, 6):
            del self.store.sets[sha(index)]
        real, width = os.pread, 2 * 8
        changed = []

        def pread(fd, length, offset):
            data = real(fd, length, offset)
            if length == width and offset == 4096 + width and not changed:  # The copy's read of set 1.
                changed.append(1)
                return bytes(data[:-1]) + bytes([data[-1] ^ 0xFF])
            return data
        self.store.reads.clear()
        with mock.patch.object(writer.os, 'pread', side_effect=pread):
            report = self.run_pass()
        self.assertEqual(changed, [1])
        self.assertEqual((report['compacted'], report['repaired'], report['remaining']), (True, 1, 0))
        self.assertEqual(self.store.reads, [sha(1)])
        with arena.opened(self.directory, **SPEC) as found:
            covered, missing, wrong = found.cover(self.store.live)
            self.assertEqual((len(covered), missing, wrong), (2, [], 0))
            self.assertEqual(bytes(found.span(*covered[sha(1)])), self.store.sets[sha(1)][2])

    def descriptors(self):
        return len(os.listdir('/dev/fd'))

    def test_a_compaction_that_fails_leaves_no_file_and_no_descriptor_behind(self):
        self.filled(count=6)
        for index in range(2, 6):
            del self.store.sets[sha(index)]
        before = sorted(path.name for path in self.directory.iterdir())
        open_before = self.descriptors()
        with mock.patch.object(writer.os, 'fsync', side_effect=OSError(28, 'No space left on device')):
            for _ in range(4):
                report = self.run_pass()
                self.assertEqual((report['skipped'], report['compacted']), ('write_unavailable', False))
                self.assertEqual(sorted(path.name for path in self.directory.iterdir()), before)
        self.assertEqual(self.descriptors(), open_before)
        self.assertEqual(len(self.cover()[0]), 2)  # The old pair still answers.
        self.assertTrue(self.run_pass()['compacted'])
        self.assertEqual(len(list(self.directory.iterdir())), 3)

    def test_a_compaction_asks_the_disk_and_the_quota_before_it_writes(self):
        self.filled(count=6)
        for index in range(2, 6):
            del self.store.sets[sha(index)]
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        opened = []
        real = os.open

        def counting(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                opened.append(path)
            return real(path, flags, *args, **kwargs)
        full = mock.Mock(f_bavail=0, f_frsize=4096)
        with mock.patch.object(writer.os, 'open', side_effect=counting):
            with mock.patch.object(writer.os, 'fstatvfs', return_value=full):
                report = self.run_pass(add=True)
            self.assertEqual((report['skipped'], report['compacted'], report['appendRows']),
                             ('write_unavailable', False, 0))
        self.assertEqual(opened, [])
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_a_compaction_is_not_held_back_by_the_quota_and_the_pass_goes_on_catching_up(self):
        # Most rows are dead and all installs together are at their bound: the copy only frees bytes.
        self.filled(count=8)
        for index in range(2, 8):
            del self.store.sets[sha(index)]
        self.store.put(20, count=1)
        own = sum(path.stat().st_size for path in self.directory.iterdir())
        os.utime(self.directory / arena.CURRENT, (1000, 1000))
        with mock.patch.object(types, 'GLOBAL_BYTES', own + 100):
            report = self.run_pass()
        self.assertEqual((report['compacted'], report['catchupRows'], report['remaining'], report['skipped'],
                          report['deadRows']), (True, 1, 0, None, 0))
        self.assertGreater(os.stat(self.directory / arena.CURRENT).st_mtime, 1000)

    def test_zeroed_rows_of_a_set_that_is_no_longer_live_are_not_left_between_the_sets(self):
        vec, _idx = self.filled(count=6)
        del self.store.sets[sha(3)]
        raw = bytearray(vec.read_bytes())
        raw[4096 + 3 * 2 * 8:4096 + 4 * 2 * 8] = b'\0' * 16  # The dead set's rows, zeroed on disk.
        vec.write_bytes(bytes(raw))
        report = self.run_pass()
        self.assertEqual((report['compacted'], report['repaired'], report['deadRows'], report['remaining']),
                         (True, 0, 0, 0))
        self.assertEqual(self.pair()[0].stat().st_size, 4096 + 5 * 2 * 8)
        # A dead set whose rows are what was written stays until the ordinary bound.
        del self.store.sets[sha(4)]
        self.assertFalse(self.run_pass()['compacted'])

    def failing_fsync(self, fail_from):
        """An ``os.fsync`` stand-in that fails from its ``fail_from``-th call on."""
        real, calls = os.fsync, []

        def fsync(fd):
            calls.append(fd)
            if len(calls) >= fail_from:
                raise OSError(5, 'Input/output error')
            real(fd)
        return mock.patch.object(writer.os, 'fsync', side_effect=fsync)

    def test_a_pass_that_fails_after_the_switch_keeps_the_new_pair_and_the_next_pass_removes_the_old_one(self):
        self.filled(count=6)
        for index in range(2, 6):
            del self.store.sets[sha(index)]
        old = self.current()['nonce']
        open_before = self.descriptors()
        # The activation syncs the vec, the idx and current.json, renames, then syncs the directory.
        with self.failing_fsync(5):
            report = self.run_pass()
        self.assertEqual(report['skipped'], 'write_unavailable')
        self.assertEqual(self.descriptors(), open_before)
        new = self.current()['nonce']
        self.assertNotEqual(new, old)
        names = sorted(path.name for path in self.directory.iterdir())
        self.assertEqual(names, sorted([arena.CURRENT, *arena.names(old), *arena.names(new)]))
        # The pair current.json names is whole: a recall answers from it.
        self.assertEqual(self.cover(), ({sha(0): (0, 2), sha(1): (2, 2)}, [], 0))
        report = self.run_pass()
        self.assertEqual((report['skipped'], report['rebuilt'], report['remaining']), (None, False, 0))
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         sorted([arena.CURRENT, *arena.names(new)]))

    def test_a_new_arena_that_cannot_be_made_leaves_nothing_behind(self):
        self.store.put(1)
        open_before = self.descriptors()
        real = os.open

        def second_fails(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and str(path).endswith('.idx'):
                raise OSError(28, 'No space left on device')
            return real(path, flags, *args, **kwargs)
        with mock.patch.object(writer.os, 'open', side_effect=second_fails):
            report = self.run_pass()
        self.assertEqual((report['skipped'], list(self.directory.iterdir()), self.descriptors()),
                         ('write_unavailable', [], open_before))
        # The pair is written but cannot be made the active one.
        with self.failing_fsync(1):
            report = self.run_pass()
        self.assertEqual((report['skipped'], list(self.directory.iterdir()), self.descriptors()),
                         ('write_unavailable', [], open_before))
        self.assertEqual(self.run_pass()['catchupRows'], 1)

    def test_a_reader_that_mapped_the_old_pair_reads_it_to_its_end_across_a_compaction(self):
        self.filled(count=4)
        with arena.opened(self.directory, **SPEC) as found:
            before = found.cover(self.store.live)[0]
            for index in range(1, 4):
                del self.store.sets[sha(index)]
            self.assertTrue(self.run_pass()['compacted'])
            first, count = before[sha(0)]
            self.assertEqual(bytes(found.span(first, count)), self.store.sets[sha(0)][2])


class TornTails(WriterCase):
    def test_an_append_after_a_torn_idx_tail_is_read(self):
        self.store.put(1, count=2)
        self.run_pass()
        vec, idx = self.pair()
        with open(idx, 'ab') as handle:
            handle.write(arena.record(sha(9), 2, 1, sha(1009), 0)[:17])  # Part of a record.
        self.store.put(2, count=3)
        report = self.run_pass()
        self.assertEqual((report['catchupRows'], report['repaired'], report['remaining'], report['compacted']),
                         (3, 0, 0, False))
        self.assertEqual(self.pair(), [vec, idx])
        self.assertEqual((idx.stat().st_size - arena.IDX_HEADER) % arena.RECORD_BYTES, 0)
        with arena.opened(self.directory, **SPEC) as found:
            covered, missing, wrong = found.cover(self.store.live)
            self.assertEqual((len(covered), missing, wrong), (2, [], 0))
            self.assertEqual(bytes(found.span(*covered[sha(2)])), self.store.sets[sha(2)][2])

    def test_vec_rows_no_record_names_are_never_appended_after(self):
        for tail in (b'\0' * 8, b'\x01\x02\x03', rows(2, 70)):  # A zero row, part of a row, whole rows.
            with self.subTest(tail=len(tail)):
                self.store.sets.clear()
                self.store.put(1, count=2)
                self.run_pass()
                vec, _idx = self.pair()
                before = vec.read_bytes()
                with open(vec, 'ab') as handle:
                    handle.write(tail)
                self.store.put(2, count=3)
                with arena.opened(self.directory, **SPEC) as reader:
                    report = self.run_pass()
                    # The old file was neither shortened nor rewritten under the recall that mapped it.
                    first, count = reader.records[sha(1)][:2]
                    self.assertEqual(bytes(reader.span(first, count)), self.store.sets[sha(1)][2])
                self.assertEqual((report['compacted'], report['catchupRows'], report['remaining']), (True, 3, 0))
                self.assertFalse(vec.exists())
                self.assertEqual(self.pair()[0].stat().st_size, 4096 + 5 * 8)  # No row between the sets.
                self.assertEqual(before[:4096 + 16][4096:], self.store.sets[sha(1)][2])
                self.assertEqual(self.cover()[1:], ([], 0))
                self.assertFalse(self.run_pass()['compacted'])

    def test_a_record_whose_rows_never_reached_the_disk_is_repaired(self):
        self.store.put(1, count=2)
        self.store.put(2, count=2)
        self.run_pass()
        vec, _idx = self.pair()
        os.truncate(vec, vec.stat().st_size - 8)  # A power cut kept the record and lost a row.
        self.assertEqual(self.cover()[2], 1)
        report = self.run_pass()
        self.assertEqual((report['repaired'], report['remaining']), (1, 0))
        self.assertEqual(self.cover()[1:], ([], 0))


class Identity(WriterCase):
    def test_a_higher_format_number_is_left_alone_and_the_pass_is_skipped(self):
        self.store.put(1)
        self.run_pass()
        value = dict(self.current(), format=arena.FORMAT + 1)
        (self.directory / arena.CURRENT).write_text(json.dumps(value))
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.store.put(2)
        report = self.run_pass(add=True)
        self.assertEqual((report['skipped'], report['catchupRows'], report['appendRows'], report['remaining']),
                         ('format_newer', 0, 0, 2))
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_another_identity_or_a_lower_format_number_is_rebuilt_under_a_new_nonce(self):
        self.store.put(1)
        self.run_pass()
        first = self.current()['nonce']
        report = self.run_pass(store_incarnation='j' * 32)
        self.assertEqual((report['rebuilt'], report['catchupRows'], report['remaining']), (True, 1, 0))
        second = self.current()['nonce']
        self.assertNotEqual(first, second)
        self.assertIsNone(self.cover())  # The first store incarnation reads "no arena" now.
        self.assertEqual(len(self.cover(store_incarnation='j' * 32)[0]), 1)
        (self.directory / arena.CURRENT).write_text(json.dumps(dict(self.current(), format=0)))
        report = self.run_pass(store_incarnation='j' * 32)
        self.assertTrue(report['rebuilt'])
        self.assertNotEqual(self.current()['nonce'], second)
        self.assertEqual(len(list(self.directory.iterdir())), 3)

    def test_an_unreadable_current_or_a_missing_pair_is_rebuilt(self):
        self.store.put(1)
        self.run_pass()
        (self.directory / arena.CURRENT).write_text('{not json')
        self.assertTrue(self.run_pass()['rebuilt'])
        self.pair()[0].unlink()
        report = self.run_pass()
        self.assertEqual((report['rebuilt'], report['catchupRows'], report['remaining']), (True, 1, 0))
        self.assertEqual(len(self.cover()[0]), 1)


class Skipped(WriterCase):
    def test_a_directory_that_cannot_be_written_skips_the_pass(self):
        self.store.put(1)
        os.chmod(self.root, 0o500)
        self.addCleanup(os.chmod, self.root, 0o700)
        report = self.run_pass(add=True)
        self.assertEqual((report['skipped'], report['catchupRows'], report['remaining']), ('write_unavailable', 0, 1))
        self.assertFalse(self.directory.exists())

    def test_an_unsafe_directory_skips_the_pass(self):
        self.store.put(1)
        self.directory.mkdir(mode=0o755)
        os.chmod(self.directory, 0o755)
        self.assertEqual(self.run_pass()['skipped'], 'write_unavailable')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_a_failing_append_skips_the_pass_and_keeps_what_was_committed(self):
        for index in range(4):
            self.store.put(index)
        real = os.fsync
        calls = []

        def failing(fd):
            calls.append(fd)
            if len(calls) > 4:  # The new pair's activation (vec, idx, current, directory) passes.
                raise OSError(28, 'No space left on device')
            real(fd)
        with mock.patch.object(writer.os, 'fsync', side_effect=failing):
            report = self.run_pass()
        self.assertEqual((report['skipped'], report['catchupRows'], report['remaining']), ('write_unavailable', 0, 4))
        self.assertEqual(self.cover()[0], {})
        again = self.run_pass()
        self.assertEqual((again['skipped'], again['catchupRows'], again['remaining']), (None, 4, 0))

    def test_sets_the_store_cannot_read_count_toward_the_bounds_of_a_pass(self):
        for index in range(6):
            self.store.put(index)
            self.store.unreadable.add(sha(index))
        with mock.patch.object(types, 'PASS_VECTORS', 2):
            report = self.run_pass(add=True)
        # Two sets asked by the catch-up and two ahead of a publication: not all six, twice.
        self.assertEqual((len(self.store.reads), report['unreadable'], report['skipped']), (4, 4, 'set_unreadable'))
        self.store.reads.clear()
        ticks = iter(range(0, 10000, 10))
        made = writer.Writer(self.directory, clock=lambda: next(ticks), **SPEC)
        self.addCleanup(made.close)
        made.start(self.store.live, self.store.read)
        self.assertEqual(len(self.store.reads), 1)  # An expired pass still asks one.

    def test_sets_a_publication_leaves_behind_are_left_to_the_catch_up(self):
        self.store.put(1)
        with mock.patch.object(types, 'PASS_VECTORS', 4):
            made = writer.Writer(self.directory, **SPEC)
            self.addCleanup(made.close)
            made.start(self.store.live, self.store.read)
            for index in range(10, 14):
                self.store.put(index, count=2)
            made.add(self.store.live, self.store.read)
        report = made.summary()
        self.assertEqual((report['catchupRows'], report['appendRows'], report['remaining'], report['skipped']),
                         (1, 4, 2, None))
        self.assertGreaterEqual(report['ms'], 0)

    def test_sets_the_store_cannot_read_are_skipped_once_the_rest_is_in(self):
        for index in range(3):
            self.store.put(index)
        self.store.unreadable.add(sha(1))
        first = self.run_pass()
        # The readable sets moved: this pass is progress, not a skip.
        self.assertEqual((first['catchupRows'], first['remaining'], first['unreadable'], first['skipped']),
                         (2, 1, 1, None))
        second = self.run_pass()
        self.assertEqual((second['catchupRows'], second['remaining'], second['skipped']), (0, 1, 'set_unreadable'))
        self.assertEqual((len(self.cover()[0]), self.cover()[1]), (2, [sha(1)]))


class Quota(WriterCase):
    def other(self, name, size, age):
        """Another install's arena of ``size`` bytes last used ``age`` seconds ago."""
        directory = self.installs / name / arena.DIRECTORY
        directory.mkdir(parents=True, mode=0o700)
        nonce = os.urandom(16).hex()
        for item, data in ((arena.names(nonce)[0], b'\0' * size), (arena.CURRENT, b'{}')):
            (directory / item).write_bytes(data)
            os.chmod(directory / item, 0o600)
        then = os.stat(directory / arena.CURRENT).st_mtime - age
        os.utime(directory / arena.CURRENT, (then, then))
        return directory

    def test_the_install_bound_stops_the_append(self):
        for index in range(4):
            self.store.put(index, count=4)
        # Twice the live bytes (256) is below the floor, and the floor has room for two sets.
        with mock.patch.object(types, 'INSTALL_FLOOR_BYTES', 4096 + 2 * 4 * 8):
            first = self.run_pass()
            second = self.run_pass()
        self.assertEqual((first['catchupRows'], first['remaining'], first['skipped']), (8, 2, None))
        self.assertEqual((second['catchupRows'], second['remaining'], second['skipped']), (0, 2, 'quota'))

    def test_over_the_global_bound_an_idle_install_is_reclaimed_oldest_first(self):
        oldest = self.other('a', 3000, OLD + 5000)
        older = self.other('b', 3000, OLD)
        recent = self.other('c', 3000, 60)
        self.store.put(1)
        with mock.patch.object(types, 'GLOBAL_BYTES', 4096 + 8 + 6100):
            report = self.run_pass()
        self.assertEqual((report['catchupRows'], report['remaining'], report['skipped']), (1, 0, None))
        self.assertEqual(list(oldest.iterdir()), [])
        self.assertEqual(len(list(older.iterdir())), 2)
        self.assertEqual(len(list(recent.iterdir())), 2)

    def test_over_the_global_bound_with_no_idle_install_the_own_arena_is_not_filled(self):
        recent = self.other('c', 9000, 60)
        self.store.put(1)
        with mock.patch.object(types, 'GLOBAL_BYTES', 9000):
            report = self.run_pass(add=True)
        self.assertEqual((report['catchupRows'], report['appendRows'], report['remaining'], report['skipped']),
                         (0, 0, 1, 'quota'))
        self.assertEqual(len(list(recent.iterdir())), 2)

    def test_a_pass_marks_the_install_as_used(self):
        self.store.put(1)
        self.run_pass()
        path = self.directory / arena.CURRENT
        os.utime(path, (1000, 1000))
        self.run_pass()
        self.assertGreater(os.stat(path).st_mtime, 1000)

    def test_files_that_are_not_the_arenas_are_neither_counted_nor_removed(self):
        idle = self.other('a', 100, OLD + 1)
        (idle / 'notes.txt').write_bytes(b'x' * 5000)
        self.store.put(1)
        with mock.patch.object(types, 'GLOBAL_BYTES', 4096 + 8 + 50):
            report = self.run_pass()
        self.assertEqual((report['catchupRows'], report['skipped']), (1, None))
        self.assertEqual([path.name for path in idle.iterdir()], ['notes.txt'])


if __name__ == '__main__':
    unittest.main()
