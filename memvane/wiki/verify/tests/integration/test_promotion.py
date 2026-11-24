"""Exercise the actual replacement verifier at the exchange of an independently editable live card."""
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.verify import promotion, verification_stamp, wiki_verify_worker as worker
from memvane.wiki.verify.tests.support import FAIL
from memvane.wiki.verify.tests.integration.test_verify_pending import PendingCase


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_save(path, data):
    temp = path.with_name('operator-save')
    with temp.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Promotion(PendingCase):
    marker = 'c' * 16
    memo = b'\n## Manual notes\nA decision written by the operator.\n'

    def verify_one(self, job):
        cfg = self.config()
        return worker.process_verify_job(self.root, cfg, cfg['compile'], worker.verify_cfg_of(cfg['compile']), job, self.compile_dir / 'verify-log.jsonl')

    def records(self):
        sidecar, = self.compile_dir.glob('retired-wiki/.promotions/*/archive.json')
        return json.loads(sidecar.read_bytes())

    def test_atomic_editor_save_preserves_the_actual_displaced_inode(self):
        job = self.stage(self.marker)
        engine = self.fake()
        exchange = promotion.rename_exchange
        edited = self.old.encode() + self.memo
        seen = []
        def save_then_exchange(*args):
            self.assertEqual(self.archived()[0].read_bytes(), self.old.encode())
            atomic_save(self.card, edited)
            seen.append(self.card.stat().st_ino)
            return exchange(*args)
        with patch.object(promotion, 'rename_exchange', side_effect=save_then_exchange):
            self.assertEqual(self.verify_one(job), (True, False))
        record = self.records()
        saved = self.root / record['archivePath']
        self.assertEqual((saved.stat().st_ino, saved.read_bytes()), (seen[0], edited))
        self.assertEqual(record['displaced']['sha256'], sha(edited))
        self.assertEqual(record['path'], job['targetPath'])
        self.assertEqual((self.root / record['basePath']).read_bytes(), self.old.encode())
        self.assertEqual(self.archived()[0].read_bytes(), self.old.encode())
        self.assertIn(b'status: verified', self.card.read_bytes())
        self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(saved.read_bytes(), edited)

    def test_open_fd_edits_at_and_after_exchange_stay_reopenable(self):
        job = self.stage(self.marker)
        self.fake()
        exchange = promotion.rename_exchange
        with self.card.open('ab') as opened:
            def edit_then_exchange(*args):
                opened.write(self.memo)
                opened.flush()
                os.fsync(opened.fileno())
                return exchange(*args)
            with patch.object(promotion, 'rename_exchange', side_effect=edit_then_exchange):
                self.assertEqual(self.verify_one(job), (True, False))
            record = self.records()
            displaced = self.root / record['archivePath']
            self.assertEqual(displaced.stat().st_ino, os.fstat(opened.fileno()).st_ino)
            self.assertEqual(record['displaced']['sha256'], sha(self.old.encode() + self.memo))
            opened.write(b'after publication\n')
            opened.flush()
            os.fsync(opened.fileno())
        self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(displaced.read_bytes(), self.old.encode() + self.memo + b'after publication\n')
        self.assertEqual((self.root / record['basePath']).read_bytes(), self.old.encode())

    def test_unavailable_exchange_keeps_live_and_retries_the_stamp_without_engine(self):
        job = self.stage(self.marker)
        fake = self.fake()
        with patch.object(promotion, 'rename_exchange', side_effect=OSError(errno.ENOTSUP, 'exchange unavailable')):
            for _ in range(2):
                self.assertEqual(self.verify_one(job), (False, True))
                self.assertEqual(self.card_now(), self.old)
                self.assertIn(b'status: verified', self.pending(self.marker).read_bytes())
                self.assertEqual(list(self.compile_dir.rglob('displaced.md')), [])
        self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(len(fake.calls), 1)

    def crash_at(self, cut):
        job = self.stage(self.marker)
        engine = self.fake()
        with patch.object(promotion, 'rename_exchange', side_effect=OSError(errno.ENOTSUP, 'initial stamp')):
            self.assertEqual(self.verify_one(job), (False, True))
        child = subprocess.run([sys.executable, '-B', '-m', __name__, str(self.root), json.dumps(self.config()),
                                json.dumps(job), cut], capture_output=True, text=True)
        self.assertEqual(child.returncode, 19, child.stderr)
        staged, = self.compile_dir.rglob('displaced.md')
        if cut == 'after':
            self.assertEqual(staged.read_bytes(), self.old.encode() + self.memo)
            self.assertNotEqual(self.card_now(), self.old)
        else:
            self.assertEqual(self.card_now(), self.old)
            self.assertIn(b'status: verified', staged.read_bytes())
        for _ in range(2):
            self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(staged.read_bytes(), self.old.encode() + (self.memo if cut == 'after' else b''))
        self.assertEqual(self.records()['archivePath'], staged.relative_to(self.root).as_posix())

    def test_process_termination_before_exchange(self):
        self.crash_at('before')

    def test_process_termination_after_exchange_before_directory_syncs(self):
        self.crash_at('after')

    def test_another_editor_save_after_a_crash_retains_both_edits(self):
        job = self.stage(self.marker)
        engine = self.fake()
        with patch.object(promotion, 'rename_exchange', side_effect=OSError(errno.ENOTSUP, 'initial stamp')):
            self.assertEqual(self.verify_one(job), (False, True))
        child = subprocess.run([sys.executable, '-B', '-m', __name__, str(self.root), json.dumps(self.config()),
                                json.dumps(job), 'after'], capture_output=True, text=True)
        self.assertEqual(child.returncode, 19, child.stderr)
        current = self.card.read_bytes() + b'second editor save\n'
        atomic_save(self.card, current)
        for _ in range(2):
            self.assertEqual(self.verify_one(job), (False, True))
        self.assertEqual(len(engine.calls), 1)
        displaced, = self.compile_dir.rglob('displaced.md')
        self.assertEqual(displaced.read_bytes(), self.old.encode() + self.memo)
        self.assertEqual(self.card.read_bytes(), current)
        self.assertTrue((displaced.parent / 'intent').is_file())
        self.assertFalse((displaced.parent / 'archive.json').exists())


class Ordinary(PendingCase):
    verify_one = Promotion.verify_one
    records = Promotion.records
    memo = Promotion.memo

    def test_first_pass_keeps_an_intervening_atomic_save(self):
        old = self.setup().encode()
        job = self.job(pendingPath=None, expectedTargetHash=None)
        engine = self.fake()
        exchange = promotion.rename_exchange
        def change(*args):
            atomic_save(self.card, old + self.memo)
            return exchange(*args)
        with patch.object(promotion, 'rename_exchange', side_effect=change):
            self.assertEqual(self.verify_one(job), (True, False))
        record = self.records()
        self.assertEqual((self.root / record['archivePath']).read_bytes(), old + self.memo)
        self.assertEqual(record['displaced']['sha256'], sha(old + self.memo))
        self.assertIn(b'status: verified', self.card.read_bytes())
        self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(len(engine.calls), 1)

    def test_rendered_contested_stamp_keeps_the_open_inode(self):
        text = self.setup()
        result = text.replace('status: generated', 'status: contested')
        exchange = promotion.rename_exchange
        with self.card.open('ab') as fd:
            def change(*args):
                fd.write(self.memo)
                fd.flush()
                os.fsync(fd.fileno())
                return exchange(*args)
            with patch.object(promotion, 'rename_exchange', side_effect=change):
                self.assertTrue(verification_stamp.write_rendered_verification(self.card, result, text))
            saved = self.root / self.records()['archivePath']
            self.assertEqual(saved.stat().st_ino, os.fstat(fd.fileno()).st_ino)
        self.assertEqual(saved.read_bytes(), text.encode() + self.memo)
        self.assertEqual(self.card_now(), result)

    def test_first_stamp_resumes_its_paid_image_after_process_termination(self):
        text = self.setup()
        job = self.job()
        engine = self.fake()
        with patch.object(promotion, 'rename_exchange', side_effect=OSError(errno.ENOTSUP, 'initial stamp')):
            self.assertEqual(self.verify_one(job), (False, True))
        child = subprocess.run([sys.executable, '-B', '-m', __name__, str(self.root), json.dumps(self.config()),
                                json.dumps(job), 'after'], capture_output=True, text=True)
        self.assertEqual(child.returncode, 19, child.stderr)
        for _ in range(2):
            self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual((self.root / self.records()['archivePath']).read_bytes(), text.encode() + self.memo)
        self.assertIn('status: verified', self.card_now())
        self.assertEqual(len(engine.calls), 1)

    def test_negative_verdict_restores_an_intervening_editor_save(self):
        text = self.setup()
        job = self.job()
        self.fake(FAIL)
        move = promotion.rename_noreplace
        def change(left, name, target, **kwargs):
            if name == self.card.name and target == 'displaced.md':
                atomic_save(self.card, text.encode() + self.memo)
            return move(left, name, target, **kwargs)
        with patch.object(promotion, 'rename_noreplace', side_effect=change):
            self.assertEqual(self.verify_one(job), (False, True))
        self.assertEqual(self.card.read_bytes(), text.encode() + self.memo)
        self.assertFalse(any(row.get('action') == 'verify-deleted' for row in self.ledger('manifest.jsonl')))

    def test_completed_negative_replays_without_touching_a_recreated_live(self):
        old = self.setup().encode()
        job = self.job()
        engine = self.fake(FAIL)
        self.assertEqual(self.verify_one(job), (True, False))
        self.assertFalse(self.card.exists())
        self.assertTrue(promotion.has_intent(self.root, job))
        saved = self.root / self.records()['archivePath']
        self.assertEqual(saved.read_bytes(), old)
        self.card.write_bytes(b'manually recreated after machine delete')
        for _ in range(2):
            self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(self.card.read_bytes(), b'manually recreated after machine delete')
        self.assertEqual(sum(row.get('action') == 'verify-deleted' for row in self.ledger('generated-manifest.jsonl')), 1)
        self.assertEqual(len(self.ledger('verify-deleted.jsonl')), 1)

    def test_negative_replays_from_the_recorded_manifest_after_a_lost_completion_marker(self):
        self.setup()
        job = self.job()
        engine = self.fake(FAIL)
        with patch.object(promotion, 'finish_retirement', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.verify_one(job)
        for _ in range(2):
            self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(len(self.ledger('generated-manifest.jsonl')), 1)

    def test_interrupted_negative_recovers_exactly_one_manifest_row_from_the_displaced_inode(self):
        old = self.setup().encode()
        job = self.job()
        engine = self.fake(FAIL)
        with patch.object(worker, 'record_machine_delete', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.verify_one(job)
        self.assertTrue(promotion.has_intent(self.root, job))
        self.assertFalse(self.card.exists())
        for _ in range(2):
            self.assertEqual(self.verify_one(job), (True, False))
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual((self.root / self.records()['archivePath']).read_bytes(), old)
        self.assertEqual([row['action'] for row in self.ledger('generated-manifest.jsonl')], ['verify-deleted'])


if __name__ == '__main__':
    root, config, job, cut = Path(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4]
    assert root.is_relative_to(Path.home())
    exchange = promotion.rename_exchange
    def crash(*args):
        if cut == 'after':
            live = root / job['targetPath']
            atomic_save(live, live.read_bytes() + Promotion.memo)
            exchange(*args)
        os._exit(19)
    with patch.object(promotion, 'rename_exchange', side_effect=crash):
        worker.process_verify_job(root, config, config['compile'], worker.verify_cfg_of(config['compile']), job, root / '.memvane/compile/verify-log.jsonl')
