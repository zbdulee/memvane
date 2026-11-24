"""The real prepare/verify entries retain an oversized generation before reading or claiming it."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.verify import api, batch_handoff, pending_handoff, wiki_verify_worker
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase, scaled
from tests.support.project import make_project


def _cut_adoption(name, timing):
    queue = Path(name)
    rename = batch_handoff.store.rename_noreplace
    def cut(parent, src, dst, **options):
        if src == queue.name and dst == 'raw':
            if timing == 'after':
                rename(parent, src, dst, **options)
            os._exit(79)
        return rename(parent, src, dst, **options)
    with patch.object(batch_handoff.store, 'rename_noreplace', side_effect=cut):
        api.prepare_queue(queue.parents[2], queue, None)


class VerifyBatchPrepare(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = make_project(self.home, git=False).root.resolve()
        self.queue = self.root / '.memvane/compile/verify-queue.jsonl'
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.bound = ({'installId': '1' * 32, 'scopeId': '2' * 32, 'storeIncarnation': '3' * 32}, {})

    def large(self, path):
        with path.open('wb') as output:
            output.write(b'opaque evidence\n')
            output.seek(8 * 1024 ** 2)
            output.write(b'unterminated')
        return path.stat()

    def prepare(self):
        return api.prepare_queue(self.root, self.queue, self.bound, {})

    def test_standalone_verify_and_prepare_retain_one_inode_without_whole_file_io(self):
        before = self.large(self.queue)
        originals = os.read, os.pread
        def check(read):
            def bounded(fd, size, *args):
                if os.fstat(fd).st_ino == before.st_ino:
                    self.assertLessEqual(size, batch_handoff.SLAB)
                return read(fd, size, *args)
            return bounded
        with patch.object(os, 'read', check(originals[0])), patch.object(os, 'pread', check(originals[1])), \
             patch.object(wiki_verify_worker, 'claim_queue', side_effect=AssertionError('claim')), \
             patch.object(workspace, 'selected', return_value=self.bound):
            result = api.run(self.root, {}, {})
            self.assertEqual((result['processed'], result['remaining']), (0, 1))
            self.assertFalse(self.prepare())
        batch, = batch_handoff.active(self.queue)
        self.assertEqual(batch.work.state['binding']['route'], workspace.route_identity(self.bound))
        held = batch.work.folder / 'raw'
        self.assertEqual((held.stat().st_ino, held.stat().st_size), (before.st_ino, before.st_size))
        self.assertNotEqual(self.queue.stat().st_ino, before.st_ino)
        self.assertEqual(self.queue.read_bytes(), b'')
        self.assertIsNone(pending_handoff._references(self.queue)[0])
        self.assertFalse(pending_handoff.enqueue_locked(self.root, {}, self.root, self.queue, {}, None, None))
        self.assertGreater(batch.cursor, 0)

    def test_prepare_resumes_process_death_on_both_sides_of_adoption(self):
        for timing in ('before', 'after'):
            with self.subTest(timing=timing):
                queue = self.tmp / timing / '.memvane/compile/verify-queue.jsonl'
                queue.parent.mkdir(parents=True)
                before = self.large(queue)
                child = subprocess.run([sys.executable, '-B', '-c',
                                        'from memvane.wiki.verify.tests.integration.test_verify_batch_prepare '
                                        'import _cut_adoption; import sys; _cut_adoption(*sys.argv[1:])',
                                        str(queue), timing], capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 79, child.stderr)
                if timing == 'after':
                    self.assertFalse(queue.exists())
                    queue.write_bytes(b'new generation\n')
                for _ in range(2):
                    self.assertFalse(api.prepare_queue(queue.parents[2], queue, None))
                batch, = batch_handoff.active(queue)
                self.assertEqual((batch.work.folder / 'raw').stat().st_ino, before.st_ino)
                self.assertEqual(queue.read_bytes(), b'new generation\n' if timing == 'after' else b'')

    def test_large_append_between_prepare_and_claim_is_sealed_under_the_claim_lock(self):
        self.queue.touch()
        inode = self.queue.stat().st_ino
        def late_queue(*args):
            self.large(self.queue)
            return None
        with patch.object(wiki_verify_worker, 'preflight_block_reason', side_effect=late_queue), \
             patch.object(workspace, 'selected', return_value=self.bound):
            result = api.run(self.root, {}, {})
        self.assertEqual((result['processed'], result['remaining']), (0, 1))
        batch, = batch_handoff.active(self.queue)
        self.assertEqual((batch.work.folder / 'raw').stat().st_ino, inode)
        self.assertEqual(self.queue.read_bytes(), b'')
        self.assertEqual(list(self.queue.parent.glob('*.claimed.*')), [])

    def test_active_claim_and_sixty_fifth_claim_never_displace_public_references(self):
        original = self.large(self.queue)
        active = self.queue.with_name(self.queue.name + '.claimed.' + str(os.getpid()) + '.live')
        active.write_bytes(b'{"targetPath":"last.md"}\n')
        self.assertFalse(self.prepare())
        self.assertEqual(self.queue.stat().st_ino, original.st_ino)
        self.assertEqual(active.read_bytes(), b'{"targetPath":"last.md"}\n')
        self.queue.write_bytes(b'new live\n')
        for i in range(64):
            self.queue.with_name(self.queue.name + '.claimed.1.extra' + str(i)).write_bytes(b'')
        self.assertFalse(self.prepare())
        self.assertEqual(self.queue.read_bytes(), b'new live\n')
        self.assertFalse((self.queue.parent / '.handoff').exists())

    def test_dead_oversized_orphan_is_captured_only_after_new_live_drains(self):
        child = subprocess.Popen([sys.executable, '-B', '-c', 'pass'])
        self.assertEqual(child.wait(timeout=30), 0)
        orphan = self.queue.with_name(self.queue.name + '.claimed.' + str(child.pid) + '.dead')
        before = self.large(orphan)
        self.queue.write_bytes(b'newer live\n')
        self.assertFalse(self.prepare())
        self.assertEqual(orphan.stat().st_ino, before.st_ino)
        self.assertEqual(self.queue.read_bytes(), b'newer live\n')
        self.queue.write_bytes(b'')
        for _ in range(2):
            self.assertFalse(self.prepare())
            self.assertEqual(self.queue.read_bytes(), b'')
        batch, = batch_handoff.active(self.queue)
        self.assertEqual((batch.work.folder / 'raw').stat().st_ino, before.st_ino)
        self.assertFalse(orphan.exists())

    def test_ambiguous_orphan_name_remains_in_place(self):
        ambiguous = self.queue.with_name(self.queue.name + '.claimed.999999999.' + str(os.getpid()) + '.running')
        before = self.large(ambiguous)
        self.assertFalse(self.prepare())
        self.assertEqual(ambiguous.stat().st_ino, before.st_ino)
        self.assertFalse((self.queue.parent / '.handoff').exists())

    def test_contended_card_and_queue_locks_return_with_original_inode(self):
        import fcntl
        before = self.large(self.queue)
        with workspace.card_write_lock(self.root):
            child = subprocess.run([sys.executable, '-B', '-c',
                                    'import sys; from pathlib import Path; from memvane.wiki.verify.api '
                                    'import prepare_queue; q=Path(sys.argv[1]); print(prepare_queue(q.parents[2],q,None))',
                                    str(self.queue)], capture_output=True, timeout=scaled(5), check=True)
            self.assertEqual(child.stdout.strip(), b'False')
        with workspace.queue_lock_path(self.queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertFalse(self.prepare())
        self.assertEqual(self.queue.stat().st_ino, before.st_ino)
        self.assertFalse((self.queue.parent / '.handoff').exists())
