"""Local trainer and shared worker failures keep their error in job telemetry.

Synthetic child processes in a private directory; telemetry is written through
``diagnostics.api.telemetry_emit`` and read back as raw JSON lines.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from memvane.diagnostics.api import telemetry_emit
from memvane.learning.context_learning.local_cycle import LocalTrainer

# The child runs briefly so the resource monitor can sample it, then fails.
FAILING_CHILD = 'import time, sys\ntime.sleep(0.35)\nsys.exit(17)\n'


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


class TrainerTelemetry(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='qmd-job-telemetry-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.script = self.root / 'fail.py'
        self.script.write_text(FAILING_CHILD)

    def test_failed_local_trainer_keeps_its_error_exit_code_and_sampled_resources(self):
        trainer = LocalTrainer([sys.executable, str(self.script)])
        with self.assertRaises(ValueError) as caught:
            trainer.call('smoke', {'schema_version': 1}, self.root / 'result.json', cwd=self.root)
        self.assertEqual(str(caught.exception), 'trainer_failed')
        self.assertEqual(trainer.last_exit_code, 17)
        path = self.root / 'job-telemetry.jsonl'
        self.assertTrue(telemetry_emit(path, 'finished', job_id='trainer:failed', op='laya_train',
                                       status='error', error_code='train_failed',
                                       exit_code=trainer.last_exit_code, resources=trainer.last_resources))
        (row,) = rows(path)
        self.assertEqual((row['status'], row['exit_code']), ('error', 17))
        samples = row.get('resource_samples')
        self.assertTrue(samples is None or samples > 0, row)
        if samples is None:
            self.assertIsNone(row.get('rss_peak_mib'))
        else:
            self.assertGreater(row['rss_peak_mib'], 0)
        self.assertNotIn('fail.py', path.read_text())
        self.assertNotIn('smoke-request', path.read_text())

    def test_live_zero_rss_snapshots_stay_unknown_and_exited_failures_keep_their_error(self):
        script = str(self.script)
        original_popen, original_run = subprocess.Popen, subprocess.run
        owned, post_exit = [], [False]

        def capture(argv, *args, **kwargs):
            process = original_popen(argv, *args, **kwargs)
            if argv[:2] == [sys.executable, script]:
                owned.append(process)
            return process

        def zero_snapshot(argv, *args, **kwargs):
            if argv == ['/bin/ps', '-axo', 'pgid=,%cpu=,rss=']:
                if post_exit[0]:
                    owned[-1].wait(timeout=2)
                return subprocess.CompletedProcess(argv, 0, str(owned[-1].pid) + ' 0.0 0\n', '')
            return original_run(argv, *args, **kwargs)

        results = []
        for required, ended in ((False, False), (True, False), (True, True)):
            post_exit[0] = ended
            trainer = LocalTrainer([sys.executable, script], require_monitor=required)
            with patch('subprocess.Popen', capture), patch('subprocess.run', zero_snapshot):
                with self.assertRaises(Exception) as caught:
                    trainer.call('smoke', {'schema_version': 1}, self.root / 'result.json', cwd=self.root)
            results.append((str(caught.exception), trainer.last_resources))
        (optional, optional_resources), (required, _), (finished, finished_resources) = results
        self.assertEqual(optional, 'trainer_failed')
        self.assertIsNone(optional_resources['resource_samples'])
        self.assertIsNone(optional_resources['observed_peak_rss_kib'])
        self.assertEqual(required, 'trainer_resource_monitor_unavailable')
        self.assertEqual(finished, 'trainer_failed')
        self.assertIsNone(finished_resources['resource_samples'])

    def test_detached_worker_records_a_failed_cycle_as_an_error(self):
        from memvane.learning.context_learning.shared import run
        path = self.root / 'job-telemetry.jsonl'
        telemetry_emit(path, 'queued', job_id='worker:1', op='shared_worker')
        with patch.dict(os.environ, {'QMD_JOB_ID': 'worker:1', 'QMD_JOB_QUEUED_AT': str(time.time() - 0.1)}), \
                patch('memvane.learning.context_learning.shared._policy', return_value={}), \
                patch('memvane.learning.context_learning.shared._run_impl', return_value={'status': 'failed'}):
            run(self.root)
        written = [row for row in rows(path) if row.get('job_id') == 'worker:1']
        self.assertEqual([row['phase'] for row in written], ['queued', 'started', 'finished'])
        self.assertEqual(written[-1]['status'], 'error')
        waits = [row['queue_wait_ms'] for row in written if row.get('queue_wait_ms') is not None]
        self.assertTrue(waits and all(wait >= 0 for wait in waits), written)


if __name__ == '__main__':
    unittest.main()
