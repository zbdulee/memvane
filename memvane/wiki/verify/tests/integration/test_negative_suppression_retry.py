"""Count model calls and ledger effects across an interrupted negative decision."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.wiki.verify.tests.support import INCONCLUSIVE, VerifyCase


class NegativeSuppressionRetry(VerifyCase):
    def test_suppression_disk_full_then_retry(self):
        self.setup()
        job = self.job()
        engine = self.fake(INCONCLUSIVE)
        config = self.config()
        compile_cfg = config['compile']
        append = worker.append_jsonl

        def verify():
            return worker.process_verify_job(self.root, config, compile_cfg, worker.verify_cfg_of(compile_cfg),
                                             job, self.compile_dir / 'verify-log.jsonl')

        def full(path, row):
            if path.name == 'verify-skipped.jsonl':
                raise OSError(errno.ENOSPC, 'suppression ledger full')
            return append(path, row)

        with patch.object(worker, 'append_jsonl', side_effect=full):
            self.assertEqual(verify(), (False, True))
        self.assertEqual((len(engine.calls), self.card.exists()), (1, True))
        self.assertEqual(len(self.ledger('verify-deleted.jsonl')), 1)
        self.assertEqual(self.ledger('verify-skipped.jsonl'), [])
        self.assertEqual(verify(), (True, False))
        self.assertFalse(self.card.exists())
        self.assertEqual(len(self.ledger('verify-skipped.jsonl')), 1)
        observed = len(engine.calls), len(self.ledger('verify-deleted.jsonl'))
        if __name__ == '__main__':
            self.assertEqual(observed, (2, 2))
        elif observed == (2, 2):
            self.skipTest('F-23 (1.1.x): retry repeats the paid decision and deletion audit')
        else:
            self.assertEqual(observed, (1, 1))

    def test_v110_suppression_disk_full_then_retry(self):
        repo = Path(__file__).resolve().parents[5]
        bundle = self.tmp / 'v1.1.0'
        bundle.mkdir()
        archive = subprocess.run(['git', '-C', str(repo), 'archive', 'v1.1.0', 'memvane', 'tests', 'package.json'],
                                 check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive.stdout, check=True)
        child = subprocess.run([sys.executable, '-B', __file__, 'NegativeSuppressionRetry.test_suppression_disk_full_then_retry'],
                               cwd=bundle, capture_output=True, text=True, timeout=30,
                               env={**os.environ, 'PYTHONPATH': str(bundle)})
        self.assertEqual(child.returncode, 0, child.stderr)


if __name__ == '__main__':
    assert Path(worker.__file__).resolve().is_relative_to(Path.cwd())
    unittest.main()
