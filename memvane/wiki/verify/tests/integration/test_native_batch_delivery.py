"""Published workers drain ranges from the same inode, including a late append after EOF."""
import json

from memvane.wiki.verify import batch_handoff
from memvane.wiki.verify.tests.support import VerifyCase


class NativeBatchDelivery(VerifyCase):
    def test_oversized_live_verifies_neighbor_then_resumes_a_torn_row_on_the_old_descriptor(self):
        self.setup()
        first = self.job()
        raw = json.dumps(first).encode() + b'\n'
        second = self.card.with_name('second.md')
        second.write_bytes(self.card.read_bytes())
        next_job = json.dumps(dict(first, targetPath=second.relative_to(self.root).as_posix())).encode()
        source = b'!' * (8 * 1024 ** 2 + 1) + b'\n' + raw + next_job[:-2]
        self.queue.write_bytes(source)
        engine = self.fake()
        with self.queue.open('ab', buffering=0) as old:
            before = self.queue.stat()
            for _ in range(400):
                self.run_verify()
                if len(engine.calls):
                    break
            self.assertEqual(len(engine.calls), 1)
            self.assert_status('verified')
            batch, = batch_handoff.active(self.queue)
            retained = batch.work.folder / 'raw'
            self.assertEqual(retained.stat().st_ino, before.st_ino)
            self.assertEqual(retained.read_bytes(), source)
            self.assertEqual(self.queue.read_bytes(), b'')
            old.write(next_job[-2:] + b'\n')
            for _ in range(400):
                self.run_verify()
                if len(engine.calls) == 2:
                    break
            self.assertEqual(len(engine.calls), 2)
            self.assertIn(b'status: verified', second.read_bytes())
            self.assertEqual(self.run_verify()['remaining'], 0)
            self.assertEqual(len(engine.calls), 2)
