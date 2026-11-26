"""Private work in wiki-pipeline.sqlite3: phase CAS, quota, archive/release and scale."""
import errno
import hashlib
import os
import sqlite3
import subprocess
import sys

from memvane.wiki.pipeline.types import DB_NAME, STAGE_QUOTA_BYTES
from memvane.wiki.verify import handoff_work as store
from tests.support.case import FixtureCase


class HandoffWorkTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.queue = self.tmp / 'verify-queue.jsonl'

    def create(self, kind='publication', **binding):
        return store.Work.create(self.queue, kind, {'route': {'scopeId': 'example'}, **binding},
                                 b'{"event":1}\n', image=b'candidate\n')

    def db(self):
        return sqlite3.connect(str(self.tmp / DB_NAME))

    def test_generation_roundtrip_and_phase_schema(self):
        work = self.create()
        self.assertEqual(len(work.folder.name), 32)
        self.assertFalse((self.tmp / '.handoff').exists())
        with self.assertRaises(ValueError):
            work.commit('published')
        with self.assertRaises(ValueError):
            work.commit('creating', binding={})
        with self.assertRaises(ValueError):
            work.commit('creating', candidate='not a file identity')
        work.commit('creating', candidate={'ino': 10})
        work.commit('staged', swapNode={'ino': 11})
        work.commit('prepared', queueBefore={}, swapDigest=hashlib.sha256(b'x').hexdigest(), holds=[], retired=[])
        found = store.Work.entries(self.queue, 'publication')
        self.assertEqual(found.issues, ())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].state, work.state)
        self.assertEqual(found[0].read('image'), b'candidate\n')
        self.assertEqual(found[0].read('raw'), b'{"event":1}\n')

    def test_two_handles_do_not_rewind_a_committed_phase(self):
        first = self.create()
        second, = store.Work.entries(self.queue, 'publication')
        first.commit('creating', candidate={'ino': 12})
        with self.assertRaisesRegex(OSError, 'changed'):
            second.commit('creating', candidate={'ino': 13})
        second.commit('creating', candidate={'ino': 12})
        self.assertEqual(second.state, first.state)
        external = first.state
        external['binding'].clear()
        self.assertEqual(first.state['binding']['route']['scopeId'], 'example')

    def test_process_exit_mid_create_leaves_no_partial_work(self):
        script = """
import os, sys
from pathlib import Path
from memvane.wiki.verify import handoff_work as store
real = store._canonical
def exit_after_validate(state):
    os._exit(72)
store._canonical = exit_after_validate
store.Work.create(Path(sys.argv[1]), 'hold', {'route': 1}, b'{"event":1}\\n', image=b'x')
"""
        child = subprocess.run([sys.executable, '-c', script, str(self.queue)], capture_output=True, text=True)
        self.assertEqual(child.returncode, 72, child.stderr)
        self.assertEqual(store.Work.entries(self.queue, 'hold'), [])
        good = store.Work.create(self.queue, 'hold', {'route': 2}, b'{"event":2}\n')
        self.assertEqual([w.state['id'] for w in store.Work.entries(self.queue, 'hold')], [good.state['id']])

    def test_quota_charges_copies_and_release_frees_them(self):
        works = [store.Work.create(self.queue, 'hold', {'purpose': 'copy', 'index': i},
                                   bytes([i]) * (STAGE_QUOTA_BYTES // 8)) for i in range(8)]
        with self.assertRaises(OSError) as raised:
            store.Work.create(self.queue, 'hold', {'purpose': 'c'}, b'y' * 32)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertIn('handoff_copy_quota', str(raised.exception))
        # A created card's own control is not a copy and is never refused.
        store.Work.create(self.queue, 'hold', {'action': 'create'}, b'z' * 1024)
        works[0].commit('held', proof={}).commit('finished').archive()
        with self.assertRaises(OSError):  # Archived work still holds its copy.
            store.Work.create(self.queue, 'hold', {'purpose': 'c'}, b'y' * 32)
        works[0].release()
        store.Work.create(self.queue, 'hold', {'purpose': 'c'}, b'y' * 32)
        self.assertFalse(list(self.tmp.glob('**/allocated-*')))

    def test_corrupt_rows_are_issues_and_other_work_is_served(self):
        good = self.create()
        bad = self.create()
        broken = self.create()
        with self.db() as db:
            db.execute("UPDATE staged_cards SET control_json = '{' WHERE stage_id = ?", (bad.state['id'],))
            db.execute("UPDATE staged_cards SET image_blob = X'00' WHERE stage_id = ?", (broken.state['id'],))
        scan = store.Work.entries(self.queue, 'publication')
        self.assertEqual([w.state['id'] for w in scan], [good.state['id']])
        self.assertEqual({row['name'] for row in scan.issues}, {bad.state['id'], broken.state['id']})
        self.assertEqual(len(store.Work.entries(self.queue, 'publication', control_only=True)), 2)
        with self.assertRaisesRegex(OSError, 'length_changed'):
            broken.read('image')

    def test_other_queue_and_kind_are_distinct(self):
        work = self.create('legacy')
        self.assertFalse(store.Work.entries(self.queue.with_name('other'), 'legacy'))
        self.assertFalse(store.Work.entries(self.queue, 'hold'))
        self.assertIsNone(store.Work.lookup(self.queue, 'hold', work.state['id']))
        with self.assertRaisesRegex(ValueError, 'queue_mismatch'):
            store.Work.lookup(self.queue.with_name('other'), 'legacy', work.state['id'])

    def test_finished_archive_reopens_by_id(self):
        work = store.Work.create(self.queue, 'hold', {'route': 1}, b'{"event":1}\n')
        with self.assertRaises(ValueError):
            work.archive()
        work.commit('held', proof={'validated': True}).commit('finished').archive()
        self.assertFalse(store.Work.entries(self.queue, 'hold'))
        saved = store.Work.lookup(self.queue, 'hold', work.state['id'])
        self.assertEqual(saved.read('raw'), b'{"event":1}\n')
        self.assertEqual(saved.state, work.state)
        saved.archive()  # Re-archiving the same closed control is a no-op.
        saved.release()
        self.assertIsNone(store.Work.lookup(self.queue, 'hold', work.state['id']))
        saved.release()  # A replayed release after the row is gone is a no-op.

    def test_inode_batch_dir_survives_until_release_and_orphan_reclaim_is_empty_only(self):
        work = store.Work._reserve(self.queue, 'hold', {'route': 1},
                                   {'raw': {'storage': 'inode', 'ino': 1, 'length': 0}}, {})
        self.assertTrue(work.folder.is_dir())
        empty = work.folder.parent / ('a' * 32)
        empty.mkdir(mode=0o700)
        holding = work.folder.parent / ('b' * 32)
        holding.mkdir(mode=0o700)
        (holding / 'raw').write_bytes(b'only copy')
        self.assertEqual(store.Work.reclaim_orphans(self.queue, 'hold', min_age_seconds=0), 1)
        self.assertFalse(empty.exists())
        self.assertTrue((holding / 'raw').exists())
        self.assertTrue(work.folder.is_dir())

    def test_hold_beyond_1024_has_no_scan_limit(self):
        ids = [store.Work.create(self.queue, 'hold', {'index': i}, b'{}\n').state['id'] for i in range(1030)]
        scan = store.Work.entries(self.queue, 'hold', control_only=True)
        self.assertEqual(scan.issues, ())
        self.assertEqual([w.state['id'] for w in scan], ids)
        last = store.Work.lookup(self.queue, 'hold', ids[-1])
        self.assertEqual(last.read('raw'), b'{}\n')

    def test_symlinked_queue_parent_is_refused(self):
        real = self.tmp / 'real'
        real.mkdir(mode=0o700)
        link = self.tmp / 'link'
        os.symlink(real, link)
        with self.assertRaisesRegex(OSError, 'unsafe'):
            store.Work.create(link / 'verify-queue.jsonl', 'hold', {}, b'{}\n')
        self.assertFalse((real / DB_NAME).exists())
