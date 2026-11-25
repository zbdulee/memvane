"""A new card, its manifest and its first private review share one recoverable operation."""
import errno
import json
import os
from unittest.mock import patch

from memvane.settings import api as settings
from memvane.wiki.verify import first_card, native_review
from memvane.wiki.verify.handoff_work import Work
from memvane.wiki.workspace import types as ct
from tests.support.case import FixtureCase


class InitialCard(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'project'
        self.root.mkdir()
        self.folder = settings.compile_dir(self.root, create=True)
        self.queue = self.folder / ct.VERIFY_QUEUE
        self.wiki = self.root / 'wiki'
        self.wiki.mkdir()
        self.path = self.wiki / 'card.md'
        self.image = b'initial card\n'
        self.job = {'targetPath': 'wiki/card.md', 'sourceHash': 'a' * 16}
        self.manifest = dict(self.job, action='created', status='generated')

    def create(self, review=True):
        return first_card.create(self.root, self.wiki, self.queue, self.job, self.image, None,
                                 review=review, manifest=self.manifest)

    def recover(self):
        first_card.recover(self.root, self.wiki, self.queue, None)

    def test_manifest_cut_retains_first_row_and_is_idempotent(self):
        with patch.object(first_card, '_record_manifest', side_effect=OSError('disk')):
            self.assertFalse(self.create())
        self.assertEqual(self.path.read_bytes(), self.image)
        pending, = Work.entries(self.queue, 'hold')
        self.assertEqual(pending.state['binding']['manifest'], self.manifest)
        self.assertEqual(pending.read('image'), self.image)
        self.assertFalse(native_review.waiting(self.queue))
        self.recover()
        self.recover()
        self.assertEqual(len(native_review.waiting(self.queue)), 1)
        manifest = settings.ledger(self.root, ct.MANIFEST)
        self.assertEqual([json.loads(line) for line in manifest.read_bytes().splitlines()], [self.manifest])
        self.assertFalse(self.queue.exists() and self.queue.read_bytes())

    def test_editor_deletion_after_first_publish_survives_an_independent_writer(self):
        child = os.fork()
        if child == 0:
            with patch.object(first_card, '_record_manifest', side_effect=lambda *_: os._exit(41)):
                self.create()
            os._exit(42)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 41)
        self.assertEqual(self.path.read_bytes(), self.image)
        self.path.unlink()
        self.recover()
        second = dict(self.job, sourceHash='b' * 16)
        self.assertFalse(first_card.create(self.root, self.wiki, self.queue, second, b'another image', None,
                                           review=True, manifest=dict(self.manifest, **second)))
        self.assertFalse(self.path.exists())
        first, = Work.entries(self.queue, 'hold')
        self.assertEqual(first.read('image'), self.image)
        self.assertFalse(native_review.waiting(self.queue))

    def test_control_write_enospc_prevents_first_exposure_and_allows_one_retry(self):
        commit = Work.commit
        def no_space(work, phase, **updates):
            if phase == 'held':
                raise OSError(errno.ENOSPC, 'full control volume')
            return commit(work, phase, **updates)
        with patch.object(Work, 'commit', no_space):
            self.assertFalse(self.create())
            self.recover()
            self.assertFalse(self.path.exists())
            self.assertFalse(native_review.waiting(self.queue))
            self.assertFalse(settings.ledger(self.root, ct.MANIFEST).exists())
        self.recover()
        self.recover()
        self.assertEqual(self.path.read_bytes(), self.image)
        job, = native_review.waiting(self.queue)
        self.assertEqual(json.loads(job.read('raw')), self.job)
        manifest = settings.ledger(self.root, ct.MANIFEST)
        self.assertEqual([json.loads(line) for line in manifest.read_bytes().splitlines()], [self.manifest])

    def test_sixty_fifth_claim_defers_first_exposure_until_references_are_known(self):
        foreign = b'{"targetPath":"wiki/other.md"}\n'
        claims = [self.queue.with_name(f'{self.queue.name}.claimed.{os.getpid()}.{i}') for i in range(65)]
        for claim in claims:
            claim.write_bytes(foreign)
        self.assertFalse(self.create())
        self.recover()
        self.assertFalse(self.path.exists())
        self.assertFalse(settings.ledger(self.root, ct.MANIFEST).exists())
        held, = Work.entries(self.queue, 'hold')
        self.assertEqual((held.read('image'), json.loads(held.read('raw'))), (self.image, self.job))
        self.assertEqual([claim.read_bytes() for claim in claims], [foreign] * 65)
        claims[-1].unlink()
        self.recover()
        self.recover()
        self.assertEqual(self.path.read_bytes(), self.image)
        self.assertEqual(len(native_review.waiting(self.queue)), 1)
        self.assertEqual([json.loads(line) for line in settings.ledger(self.root, ct.MANIFEST).read_bytes().splitlines()], [self.manifest])

    def test_no_replace_after_editor_takes_initial_name(self):
        def editor(parent, source, target):
            self.path.write_bytes(b'operator copy\n')
            raise FileExistsError
        with patch.object(first_card, 'rename_noreplace', side_effect=editor):
            self.assertFalse(self.create())
        self.recover()
        self.assertEqual(self.path.read_bytes(), b'operator copy\n')
        self.assertFalse(native_review.waiting(self.queue))
        self.assertTrue(native_review.busy(self.queue))
        work, = Work.entries(self.queue, 'hold')
        self.assertEqual(work.read('image'), self.image)

    def test_recreated_visible_inode_is_preserved_even_with_the_same_bytes(self):
        from memvane.wiki.verify import wiki_verify_worker as verify
        self.assertTrue(self.create())
        other = self.path.with_name('editor-save')
        other.write_bytes(self.image)
        other.replace(self.path)
        with patch.object(verify, 'process_verify_job') as verify_job:
            result = verify.run(self.root, {'wikiPath': 'wiki'}, {'verify': {'enabled': True}})
        self.assertEqual(result['remaining'], 1)
        verify_job.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.image)

    def test_released_ordinary_reference_blocks_first_exposure(self):
        claimed = self.queue.with_name(self.queue.name + '.claimed.12345.legacy')
        claimed.write_bytes(json.dumps(self.job).encode() + b'\n')
        self.assertFalse(self.create())
        self.assertFalse(self.path.exists())
        claimed.unlink()
        self.recover()
        self.assertEqual(self.path.read_bytes(), self.image)
        self.assertEqual(len(native_review.waiting(self.queue)), 1)

    def test_unregistered_temp_survives_and_retry_creates_independent_inode(self):
        real = Work.commit
        def stop(work, phase, **updates):
            if phase == 'held' and 'inode' in updates['proof']:
                raise KeyboardInterrupt
            return real(work, phase, **updates)
        with patch.object(Work, 'commit', stop), self.assertRaises(KeyboardInterrupt):
            self.create(review=False)
        unregistered, = self.wiki.glob('.first-*')
        self.recover()
        self.assertNotEqual(self.path.stat().st_ino, unregistered.stat().st_ino)
        archived, = (self.folder / '.handoff/v1/closed/hold').glob('*/work')
        self.assertIn(unregistered.name, json.loads(archived.read_bytes())['proof']['unknown'])
        self.assertEqual(unregistered.read_bytes(), self.image)
        self.assertFalse(native_review.busy(self.queue))

    def test_orphan_hold_and_over_64_valid_holds_recover_and_publish_first_cards(self):
        import subprocess, sys, time
        from memvane.wiki.verify import queue_batch_prepare
        script = """
import os, sys
from pathlib import Path
from memvane.wiki.verify import handoff_work as store
q, stage = sys.argv[1:]
if stage == 'publish':
    real_rename = store.rename_noreplace
    def stop(parent, src, dst, **kwargs):
        if dst == 'raw':
            os._exit(74)
        return real_rename(parent, src, dst, **kwargs)
    store.rename_noreplace = stop
else:
    store._save = lambda *args, **kwargs: os._exit(74)
store.Work.create(q, 'hold', {'route': {'scopeId': 'example'}, 'stage': stage}, b'{"event":1}\\n', image=b'candidate\\n')
"""
        for stage in ('publish', 'save'):
            child = subprocess.run([sys.executable, '-B', '-c', script, str(self.queue), stage], capture_output=True)
            self.assertEqual(child.returncode, 74, child.stderr)
        for index in range(65):
            Work.create(self.queue, 'hold', {'purpose': 'unrelated', 'ordinal': index}, b'{}\n').commit('held')
        with patch.object(first_card, '_released_target_clear', return_value=False):
            self.assertFalse(self.create())
        self.assertFalse(self.path.exists())
        self.assertTrue(queue_batch_prepare.unresolved(self.queue))
        hold_dir = self.folder / '.handoff' / 'v1' / 'hold'
        self.assertEqual(len([p for p in hold_dir.iterdir() if not (p / 'work').exists()]), 2)
        old = time.time() - 120
        for p in hold_dir.iterdir():
            if not (p / 'work').exists():
                for item in p.iterdir():
                    os.utime(item, (old, old))
                os.utime(p, (old, old))
        self.recover()
        self.assertFalse(queue_batch_prepare.unresolved(self.queue))
        self.assertEqual([p for p in hold_dir.iterdir() if not (p / 'work').exists()], [])
        self.assertEqual(list(self.wiki.glob('.first-*')), [])
        self.assertEqual(self.path.read_bytes(), self.image)
        manifest = settings.ledger(self.root, ct.MANIFEST)
        self.assertEqual([json.loads(line) for line in manifest.read_bytes().splitlines()], [self.manifest])
