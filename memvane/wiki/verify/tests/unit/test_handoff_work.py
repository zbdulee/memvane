"""Private work snapshots, interrupted commits, unsafe references and scan evidence."""
import hashlib
import subprocess
import sys
import os
import stat
from unittest.mock import patch

from memvane.wiki.verify import handoff_work as store
from tests.support.case import FixtureCase


class HandoffWorkTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.queue = self.tmp / 'verify-queue.jsonl'

    def create(self, kind='publication'):
        return store.Work.create(self.queue, kind, {'route': {'scopeId': 'example'}},
                                 b'{"event":1}\n', image=b'candidate\n')

    def test_generation_roundtrip_and_phase_schema(self):
        work = self.create()
        self.assertEqual(len(work.folder.name), 32)
        self.assertEqual(stat.S_IMODE(work.folder.stat().st_mode), 0o700)
        for blob in ('work', 'raw', 'image'):
            self.assertEqual(stat.S_IMODE((work.folder / blob).stat().st_mode), 0o600)
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

    def test_incomplete_generation_keeps_raw_and_independent_create_succeeds(self):
        script = """
import os, sys
from pathlib import Path
from memvane.wiki.verify import handoff_work as store
q = Path(sys.argv[1])
store._save = lambda *args, **kwargs: os._exit(72)
store.Work.create(q, 'hold', {'route': {'scopeId': 'example'}}, b'{"event":1}\\n', image=b'candidate\\n')
"""
        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.queue)], capture_output=True)
        self.assertEqual(child.returncode, 72, child.stderr)
        good = self.create('hold')
        scan = store.Work.entries(self.queue, 'hold')
        self.assertEqual([w.folder for w in scan], [good.folder])
        self.assertEqual(len(scan.issues), 1)
        orphan = good.folder.parent / scan.issues[0]['name']
        self.assertEqual((orphan / 'raw').read_bytes(), b'{"event":1}\n')
        self.assertEqual((orphan / 'image').read_bytes(), b'candidate\n')
        self.assertFalse((orphan / 'work').exists())

    def test_reserve_failure_rolls_back_temp_dir_and_copy_quota(self):
        from memvane.wiki.verify import copy_quota
        from memvane.wiki.workspace.handoff import api as handoff_api
        hold_dir = self.queue.parent / '.handoff' / 'v1' / 'hold'
        v1 = self.queue.parent / '.handoff' / 'v1'
        with handoff_api.budget.scope() as ledger:
            ledger.debit('private', ledger.available('private') - 10)
            with self.assertRaisesRegex(BlockingIOError, 'handoff_private_pass_limit'):
                self.create('hold')
        self.assertEqual(list(hold_dir.iterdir()), [])
        self.assertEqual(list(v1.glob('allocated-*')), [])
        with store.private_root(self.queue) as root:
            self.assertEqual(copy_quota._used(root), 0)
        with patch.object(store, '_save', side_effect=OSError('save failed')):
            with self.assertRaisesRegex(OSError, 'save failed'):
                self.create('hold')
        self.assertEqual(list(hold_dir.iterdir()), [])
        self.assertEqual(list(v1.glob('allocated-*')), [])
        with store.private_root(self.queue) as root:
            self.assertEqual(copy_quota._used(root), 0)
        fsync = os.fsync
        def fail_after_work_rename(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and store._exists(fd, 'work'):
                raise OSError('fsync after work')
            fsync(fd)
        with patch.object(os, 'fsync', side_effect=fail_after_work_rename):
            with self.assertRaisesRegex(OSError, 'fsync after work'):
                self.create('hold')
        self.assertEqual(len(store.Work.entries(self.queue, 'hold')), 1)

    def test_reclaim_orphans_checks_age_unknown_files_and_releases_quota(self):
        import time
        from memvane.wiki.verify import copy_quota
        script = """
import os, sys
from pathlib import Path
from memvane.wiki.verify import handoff_work as store
q, stage = sys.argv[1:]
if stage == 'publish':
    real_rename = store.rename_noreplace
    def stop(parent, src, dst, **kwargs):
        if dst == 'raw':
            os._exit(73)
        return real_rename(parent, src, dst, **kwargs)
    store.rename_noreplace = stop
else:
    store._save = lambda *args, **kwargs: os._exit(73)
store.Work.create(q, 'hold', {'route': {'scopeId': 'example'}, 'stage': stage}, b'{"event":1}\\n', image=b'candidate\\n')
"""
        for stage in ('publish', 'save'):
            child = subprocess.run([sys.executable, '-B', '-c', script, str(self.queue), stage], capture_output=True)
            self.assertEqual(child.returncode, 73, child.stderr)
        good = self.create('hold')
        hold_dir = good.folder.parent
        orphans = [p for p in hold_dir.iterdir() if p != good.folder]
        self.assertEqual(len(orphans), 2)
        self.assertEqual(store.Work.reclaim_orphans(self.queue, 'hold'), 0)
        future = time.time() + 300
        for p in orphans:
            for item in p.iterdir():
                os.utime(item, (future, future))
            os.utime(p, (future, future))
        self.assertEqual(store.Work.reclaim_orphans(self.queue, 'hold'), 0)
        kept = orphans[0]
        (kept / 'manual.txt').write_text('keep')
        old = time.time() - 120
        for p in orphans:
            for item in p.iterdir():
                os.utime(item, (old, old))
            os.utime(p, (old, old))
        self.assertEqual(store.Work.reclaim_orphans(self.queue, 'hold'), 1)
        self.assertTrue(kept.exists())
        (kept / 'manual.txt').unlink()
        os.utime(kept, (old, old))
        self.assertEqual(store.Work.reclaim_orphans(self.queue, 'hold'), 1)
        self.assertEqual(list(hold_dir.iterdir()), [good.folder])
        with store.private_root(self.queue) as root:
            self.assertEqual(copy_quota._used(root), len(b'{"event":1}\n') + len(b'candidate\n'))

    def test_invalid_control_blob_and_unknown_version_remain_byte_exact(self):
        bad = self.create()
        (bad.folder / 'raw').write_bytes(b'{"event":2}\n')
        broken = self.create()
        broken.path.write_bytes(b'{"unfinished"')
        unknown = self.create()
        state = unknown.state
        state['version'] = 2
        unknown.path.write_bytes(store.canonical_json_bytes(dict(state, checksum=store._sha(store.canonical_json_bytes(state)))))
        original = {w.path: w.path.read_bytes() for w in (bad, broken, unknown)}
        good = self.create()
        scan = store.Work.entries(self.queue, 'publication')
        self.assertEqual([w.folder for w in scan], [good.folder])
        self.assertEqual({row['name'] for row in scan.issues}, {w.folder.name for w in (bad, broken, unknown)})
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        with self.assertRaisesRegex(OSError, 'digest_changed'):
            bad.read('raw')

    def test_symlink_hardlink_and_mode_refusal_preserve_other_files(self):
        work = self.create()
        other = self.tmp / 'other'
        other.write_bytes(work.read('raw'))
        other.chmod(0o600)
        raw = work.folder / 'raw'
        raw.unlink()
        raw.symlink_to(other)
        self.assertTrue(store.Work.entries(self.queue, 'publication').issues)
        raw.unlink()
        os.link(other, raw)
        self.assertTrue(store.Work.entries(self.queue, 'publication').issues)
        raw.unlink()
        raw.write_bytes(other.read_bytes())
        raw.chmod(0o644)
        self.assertTrue(store.Work.entries(self.queue, 'publication').issues)
        self.assertEqual(other.read_bytes(), b'{"event":1}\n')

    def test_budget_exhaustion_is_unknown_and_other_queue_is_distinct(self):
        work = self.create('legacy')
        work.commit('imported', sourceDigest=store._sha(b'old'), imported={'items': []}, proof={})
        self.assertFalse(store.Work.entries(self.queue.with_name('other'), 'legacy'))
        scan = store.Work.entries(self.queue, 'legacy', max_bytes=5)
        self.assertFalse(scan)
        self.assertEqual(scan.issues[0]['reason'], 'handoff_scan_limit')
        self.assertTrue(work.path.is_file())
        with self.assertRaises(ValueError):
            work.commit('imported', cursor=True)

    def test_retry_after_directory_fsync_failure_accepts_only_same_control(self):
        work = self.create()
        replace, fsync = os.replace, os.fsync
        replaced = []
        def publish(*args, **kwargs):
            replace(*args, **kwargs)
            replaced.append(True)
        def interrupt(fd):
            if replaced:
                raise OSError('after rename')
            fsync(fd)
        with patch.object(os, 'replace', side_effect=publish), patch.object(os, 'fsync', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'after rename'):
                work.commit('creating', candidate={'inode': 12})
        work.commit('creating', candidate={'inode': 12})
        self.assertEqual(work.state['phase'], 'creating')
        self.assertEqual(store.Work.entries(self.queue, 'publication')[0].state, work.state)

    def test_finished_archive_reopens_by_id_outside_active_limit(self):
        work = self.create('hold')
        identity = work.folder.name
        inode = (work.folder / 'raw').stat().st_ino
        with self.assertRaises(ValueError):
            work.archive()
        work.commit('held', proof={'input': 'queue'}).commit('finished')
        work.archive().archive()
        self.assertFalse(store.Work.entries(self.queue, 'hold', max_entries=0).issues)
        saved = store.Work.lookup(self.queue, 'hold', identity)
        self.assertEqual(saved.read('raw'), b'{"event":1}\n')
        self.assertEqual(saved.folder, work.folder)
        self.assertEqual((saved.folder / 'raw').stat().st_ino, inode)

    def test_release_replays_after_blob_unlink_and_preserves_unknown_file(self):
        work = self.create('hold')
        work.commit('held').commit('finished').archive()
        decoy = work.folder / 'manual-file'
        decoy.write_text('keep')
        with self.assertRaisesRegex(OSError, 'unknown_handoff_release_file'):
            work.release()
        self.assertTrue((work.folder / 'raw').exists())
        self.assertEqual(decoy.read_text(), 'keep')
        decoy.unlink()
        unlink = os.unlink
        def interrupted(path, **options):
            unlink(path, **options)
            if path == 'raw':
                raise OSError('interrupted')
        with patch.object(os, 'unlink', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                work.release()
        resumed = store.Work.lookup(self.queue, 'hold', work.folder.name)
        resumed.release()
        self.assertFalse(work.folder.exists())
        self.assertIsNone(store.Work.lookup(self.queue, 'hold', work.folder.name))

    def test_unrecognized_generation_preserved_with_independent_work(self):
        unknown = self.queue.parent / '.handoff/v9'
        unknown.mkdir(mode=0o700, parents=True)
        unknown.parent.chmod(0o700)
        (unknown / 'work').write_bytes(b'v9 raw')
        self.assertEqual(store.Work.entries(self.queue, 'hold').issues[0]['reason'], 'unknown_handoff_generation')
        work = self.create('hold')
        scan = store.Work.entries(self.queue, 'hold')
        self.assertEqual(scan[0].read('raw'), work.read('raw'))
        self.assertTrue(scan.issues)
        self.assertEqual((unknown / 'work').read_bytes(), b'v9 raw')


    def test_actual_process_exit_on_either_side_of_archive_rename(self):
        script = """
import os, sys
from pathlib import Path
from memvane.wiki.verify import handoff_work as store
q, identifier, when = sys.argv[1:]
work = store.Work.lookup(Path(q), 'hold', identifier)
real = store.rename_noreplace
def stop(parent, source, target, **options):
    if source == identifier and target == identifier:
        if when == 'after':
            real(parent, source, target, **options)
        os._exit(71)
    return real(parent, source, target, **options)
store.rename_noreplace = stop
work.archive()
"""
        for point in ('before', 'after'):
            with self.subTest(point=point):
                work = self.create('hold')
                work.commit('held').commit('finished')
                identity = work.folder.name
                child = subprocess.run([sys.executable, '-B', '-c', script, str(self.queue), identity, point],
                                       capture_output=True)
                self.assertEqual(child.returncode, 71, child.stderr)
                restored = store.Work.lookup(self.queue, 'hold', identity)
                self.assertEqual(restored.read('raw'), b'{"event":1}\n')
                restored.archive().archive()
                self.assertFalse(store.Work.entries(self.queue, 'hold'))

    def test_the_65th_active_is_explicitly_unknown(self):
        for index in range(65):
            store.Work.create(self.queue, 'hold', {'ordinal': index}, b'{}\n')
        scan = store.Work.entries(self.queue, 'hold')
        self.assertEqual(len(scan), 64)
        self.assertEqual([r['reason'] for r in scan.issues], ['handoff_scan_limit'])
        missing = store.Work.lookup(self.queue, 'hold', scan.issues[0]['name'])
        self.assertEqual(missing.read('raw'), b'{}\n')
