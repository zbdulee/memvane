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

    def sidecar_child(self, body):
        """A trainer child that writes ``body`` (a Python expression) as its failure sidecar."""
        script = self.root / 'sidecar.py'
        script.write_text('import json, os, sys\npath = sys.argv[-1]\n'
                          'fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n'
                          'os.write(fd, json.dumps(%s).encode())\nos.close(fd)\nsys.exit(1)\n' % body)
        return LocalTrainer([sys.executable, str(script)])

    def fail(self, body):
        trainer = self.sidecar_child(body)
        with self.assertRaises(Exception) as caught:
            trainer.call('smoke', {'schema_version': 1}, self.root / 'result.json', cwd=self.root)
        return trainer, caught.exception

    def test_a_trainer_failure_carries_the_child_error_site(self):
        from memvane.kernel.resource_deferred import ResourceDeferred
        site = ['adapter.py:869 <module>', 'adapter.py:824 main', 'adapter.py:445 training_guard',
                'resource_coordination.py:363 __init__']
        trainer, error = self.fail({'schema_version': 1, 'error_code': 'learning_control_invalid:root',
                                    'error_site': site})
        self.assertEqual((type(error), str(error)), (ValueError, 'trainer_failed:learning_control_invalid:root'))
        self.assertEqual((error.error_site, trainer.last_error_site, trainer.last_error_code),
                         (site, site, 'learning_control_invalid:root'))
        # A deferral keeps it in its report, which the worker's resource record stores.
        trainer, error = self.fail({'schema_version': 1, 'error_code': 'learning_yield', 'error_site': site[-1:]})
        self.assertEqual((type(error), str(error)), (ResourceDeferred, 'trainer_failed:learning_yield'))
        self.assertEqual(error.report, {'reason': 'learning_yield', 'error_site': site[-1:]})
        stored = json.loads((self.root / 'resource-defer.json').read_text())
        self.assertEqual(stored['resources']['error_site'], site[-1:])
        # A sidecar without the key (or the three control codes' old spelling) still reads.
        for code in ('learning_control_invalid', 'learning_control_invalid:keys',
                     'learning_control_invalid:stop_fd', 'invalid_case'):
            with self.subTest(code=code):
                trainer, error = self.fail({'schema_version': 1, 'error_code': code})
                self.assertEqual((str(error), error.error_site, trainer.last_error_site),
                                 ('trainer_failed:' + code, None, None))

    def test_an_error_site_keeps_its_frames_and_leaves_out_the_other_entries(self):
        secret = 'The emerald heron case text'
        frames = ['adapter.py:869 <module>', 'adapter.py:824 main']
        # An import failure: the frozen importlib frame has a space in its file name.
        mixed = [frames[0], '<frozen importlib._bootstrap>:1050 _gcd_import', secret, 7, frames[1],
                 '/Users/someone/private/adapter.py:1 main']
        trainer, error = self.fail({'schema_version': 1, 'error_code': 'invalid_case', 'error_site': mixed})
        self.assertEqual((str(error), error.error_site, trainer.last_error_site),
                         ('trainer_failed:invalid_case', frames, frames))
        # At most eight frames are kept.
        trainer, error = self.fail({'schema_version': 1, 'error_code': 'invalid_case',
                                    'error_site': ['adapter.py:%d main' % line for line in range(1, 12)]})
        self.assertEqual(error.error_site, ['adapter.py:%d main' % line for line in range(1, 9)])
        trainer, error = self.fail({'schema_version': 1, 'error_code': 'learning_yield', 'error_site': mixed})
        self.assertEqual(error.report, {'reason': 'learning_yield', 'error_site': frames})
        self.assertNotIn('heron', (self.root / 'resource-defer.json').read_text())

    def test_an_error_site_that_is_not_frames_is_dropped_and_never_stored(self):
        secret = 'The emerald heron case text'
        for bad in ([secret], ['/Users/someone/private/adapter.py:1 main'],
                    'adapter.py:12 main', [], [7], ['adapter.py:x main'], ['adapter.py:12 main\nmore']):
            with self.subTest(bad=bad):
                trainer, error = self.fail({'schema_version': 1, 'error_code': 'invalid_case',
                                            'error_site': bad})
                # The audited code survives; the unusable site does not.
                self.assertEqual((str(error), error.error_site, trainer.last_error_site),
                                 ('trainer_failed:invalid_case', None, None))
        trainer, error = self.fail({'schema_version': 1, 'error_code': 'learning_yield', 'error_site': [secret]})
        self.assertEqual(error.report, {'reason': 'learning_yield'})
        self.assertNotIn('heron', (self.root / 'resource-defer.json').read_text())
        # An unknown key or code keeps the generic failure, as before.
        for body in ({'schema_version': 1, 'error_code': 'invalid_case', 'note': secret},
                     {'schema_version': 1, 'error_code': 'learning_control_invalid:elsewhere'}):
            trainer, error = self.fail(body)
            self.assertEqual((str(error), trainer.last_error_code), ('trainer_failed', 'trainer_process_failed'))

    def test_the_adapter_names_frames_only_and_keeps_the_control_codes(self):
        from memvane.learning.context_learning import adapter

        def inner(text):
            raise ValueError('learning_control_invalid:root')

        def outer(text):
            inner(text)
        secret = 'case text with the emerald heron'
        try:
            outer(secret)
        except ValueError as exc:
            site = adapter._error_site(exc)
            code = adapter._safe_error(exc)
        self.assertEqual(code, 'learning_control_invalid:root')
        self.assertEqual([entry.rsplit(' ', 1)[1] for entry in site],
                         ['test_the_adapter_names_frames_only_and_keeps_the_control_codes', 'outer', 'inner'])
        for entry in site:
            self.assertRegex(entry, r'^test_learning_trainer_telemetry\.py:\d+ \w+$')
        self.assertNotIn('heron', json.dumps(site))
        self.assertNotIn(os.sep, json.dumps(site))
        self.assertEqual(adapter._safe_error(ValueError('learning_control_invalid:' + secret)),
                         'adapter_unclassified_error')
        self.assertEqual(adapter._safe_error(ValueError(secret)), 'adapter_unclassified_error')

        from memvane.learning.context_learning import training_watch
        with open(os.devnull) as plain, self.assertRaises(ValueError) as caught:
            training_watch.poll_stop(plain.fileno())
        self.assertEqual(str(caught.exception), 'learning_control_invalid:stop_fd')

        def deep(depth):
            if depth:
                return deep(depth - 1)
            raise RuntimeError(secret)
        try:
            deep(30)
        except RuntimeError as exc:
            self.assertEqual(len(adapter._error_site(exc)), 8)

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
