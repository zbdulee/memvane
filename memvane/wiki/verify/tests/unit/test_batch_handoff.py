"""An adopted inode is read by range and owes every byte until durable handoff proof."""
import os
from unittest.mock import patch

from memvane.wiki.verify import batch_handoff as batches, handoff_work as store
from tests.support.case import FixtureCase


class BatchHandoffTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.queue = self.tmp / 'verify-queue.jsonl'

    def test_adopt_large_inode_and_lookup_never_read_raw_in_full(self):
        size = 12 * 1024 ** 2
        with self.queue.open('wb') as handle:
            handle.truncate(size)
        inode = self.queue.stat().st_ino
        calls = []
        read = os.read
        def checked(fd, request):
            self.assertLessEqual(request, batches.SLAB)
            self.assertNotEqual(os.fstat(fd).st_ino, inode)
            calls.append(request)
            return read(fd, request)
        with patch.object(os, 'read', side_effect=checked):
            batch = batches.adopt(self.queue)
            found = store.Work.lookup(self.queue, 'hold', batch.work.folder.name)
            self.assertEqual(found.state['blobs']['raw']['length'], size)
            self.assertEqual(len(batches.active(self.queue)), 1)
            with self.assertRaisesRegex(ValueError, 'requires_window'):
                found.read('raw')
        self.assertEqual((batch.work.folder / 'raw').stat().st_ino, inode)
        self.assertNotEqual(self.queue.stat().st_ino, inode)
        self.assertEqual(self.queue.read_bytes(), b'')
        before = batch.read_window()
        self.assertEqual(len(before.data), batches.SLAB)
        batch.commit_window(before, {'owner': 'retained-one'}, end=123)
        self.assertEqual(batches.active(self.queue)[0].cursor, 123)
        with self.assertRaisesRegex(BlockingIOError, 'eof_unproven'):
            batch.finish({'consumer': 'done'})

    def test_other_claim_and_live_owner_are_preserved(self):
        self.queue.write_bytes(b'live\n')
        orphan = self.queue.with_name(self.queue.name + '.claimed.42.dead')
        orphan.write_bytes(b'orphan\n')
        with self.assertRaisesRegex(BlockingIOError, 'unobserved_claim'):
            batches.adopt(self.queue)
        with self.assertRaisesRegex(BlockingIOError, 'not_proven_dead'):
            batches.adopt(self.queue, orphan)
        batch = batches.adopt(self.queue, orphan, orphan_dead=lambda path: path == orphan)
        self.assertEqual(batch.read_window().data, b'orphan\n')
        self.assertEqual(self.queue.read_bytes(), b'live\n')

    def test_crash_before_and_after_rename_resume_without_reading_new_live(self):
        for after in (False, True):
            with self.subTest(after=after):
                queue = self.queue.with_name('queue-' + str(after))
                queue.write_bytes(b'work\n')
                real = store.rename_noreplace
                def interrupt(parent, src, dst, **opts):
                    if src == queue.name and dst == 'raw':
                        if after:
                            real(parent, src, dst, **opts)
                        raise OSError('cut')
                    real(parent, src, dst, **opts)
                with patch.object(store, 'rename_noreplace', side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, 'cut'):
                        batches.adopt(queue)
                if after:
                    queue.write_bytes(b'new live\n')
                batch, = batches.active(queue)
                self.assertEqual(batch.read_window().data, b'work\n')
                self.assertEqual(queue.read_bytes(), b'new live\n' if after else b'')

    def test_late_append_revalidates_previously_acknowledged_bytes_by_slab(self):
        initial = b'a' * (batches.SLAB + 10)
        self.queue.write_bytes(initial)
        with self.queue.open('ab', buffering=0) as legacy:
            batch = batches.adopt(self.queue)
            first = batch.read_window()
            batch.commit_window(first, {'owner': 'first range'})
            second = batch.read_window()
            batch.commit_window(second, {'owner': 'second range'})
            legacy.write(b'late\n')
        self.assertIsNone(batch.read_window())
        self.assertIsNone(batch.read_window())
        tail = batch.read_window()
        self.assertEqual(tail.offset, len(initial))
        self.assertEqual(tail.data, b'late\n')
        with self.assertRaisesRegex(ValueError, 'no_proof'):
            batch.commit_window(tail, {})
        batch.commit_window(tail, {'owner': 'late range'})
        batch.finish({'owner': 'all ranges'})
        self.assertFalse(batches.active(self.queue))
        saved = store.Work.lookup(self.queue, 'hold', batch.work.folder.name)
        self.assertEqual((saved.folder / 'raw').stat().st_size, len(initial) + 5)
        saved.release()
        self.assertIsNone(store.Work.lookup(self.queue, 'hold', batch.work.folder.name))

    def test_changed_acknowledged_prefix_stays_manual_and_cannot_advance(self):
        self.queue.write_bytes(b'initial\nmore\n')
        with self.queue.open('r+b', buffering=0) as old:
            batch = batches.adopt(self.queue)
            window = batch.read_window()
            batch.commit_window(window, {'owner': 'one'}, end=8)
            old.write(b'changed\n')
        with self.assertRaisesRegex(OSError, 'acknowledged_prefix_changed'):
            batch.read_window()
        self.assertEqual(batch.work.state['phase'], 'manual')
        self.assertEqual(batch.work.state['cursor'], 8)
        self.assertTrue(batches.active(self.queue).issues)
        self.assertTrue((batch.work.folder / 'raw').is_file())
