import contextlib
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

from entry import run as entry
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from memvane.diagnostics import diagnostic_trace
from memvane.runtime import api as runtime


class DispatchWorkerDiagnosticsTests(FixtureCase):
    def dispatch_fixture(self, mode, *, capture_fails=False):
        project = make_project(self.home, git=False)
        install = build_install(self, project)
        settings = json.loads(project.settings_path.read_text())
        settings['diagnostics'] = {'enabled': True}
        project.settings_path.write_text(json.dumps(settings))
        fake = self.tmp / 'synthetic-child.py'
        if mode == 'preparing':
            fake.write_text('import json\nprint(json.dumps({"state":"deferred","reason":"laya_service_preparing"}))\n')
        elif mode == 'indexed':
            fake.write_text('import json\nprint(json.dumps({"state":"indexed","generation":"g2"}))\n')
        else:
            fake.write_text('import sys\nsys.stderr.write("synthetic-child-private-error\\n")\nsys.exit(17)\n')
        queue = runtime.wake_queue()
        self.assertEqual(queue.offer('laya-sync', str(project.root)), 'queued')
        lease = os.open(queue.root / 'runtime-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
        before = len(install.events())
        real_popen = subprocess.Popen
        spawned = []
        def controlled_popen(args, *pargs, **kwargs):
            if isinstance(args, (list, tuple)) and 'wake' in args:
                spawned.append('wake')
                if mode == 'exception':
                    raise RuntimeError('synthetic-dispatch-error')
                return real_popen([sys.executable, '-B', str(fake)], *pargs, **kwargs)
            if isinstance(args, (list, tuple)) and 'runtime-work' in args:
                raise OSError('synthetic fixture does not start background workers')
            return real_popen(args, *pargs, **kwargs)
        argv = ['entry/run.py', 'runtime-work', 'worker', str(lease)]
        capture = (patch('memvane.diagnostics.api.capture', side_effect=OSError('logging failed'))
                   if capture_fails else contextlib.nullcontext())
        try:
            with patch.object(sys, 'argv', argv), patch('subprocess.Popen', side_effect=controlled_popen), \
                 patch('memvane.learning.api.wake_after_index', return_value=None), \
                 capture, contextlib.redirect_stdout(io.StringIO()):
                entry.main()
        finally:
            try:
                os.close(lease)
            except OSError:
                pass
        self.assertEqual(spawned, ['wake'])
        rows = install.events()[before:]
        event = next(row for row in rows if row.get('event') == 'wake.sync')
        return event, rows

    def test_successful_sync_records_wake_and_publish(self):
        event, rows = self.dispatch_fixture('indexed')
        self.assertEqual((event['outcome'], event['generation']), ('indexed', 'g2'))
        publish = [row for row in rows if row.get('event') == 'index.publish']
        self.assertEqual(len(publish), 1)
        self.assertEqual(publish[0]['generation'], 'g2')

    def test_child_failure_records_bounded_tail_without_exposing_it_in_event(self):
        event, rows = self.dispatch_fixture('child')
        self.assertEqual((event['outcome'], event['reason'], event['trace_unavailable']),
                         ('failed', 'target_child_failed', 'child_process'))
        self.assertNotIn('synthetic-child-private-error', json.dumps(event))
        detail = diagnostic_trace.reassemble(rows, event['diagnostic_id'])
        self.assertEqual((detail['status'], detail['kind']), ('complete', 'child_stderr_tail'))
        self.assertIn('synthetic-child-private-error', detail['text'])
        self.assertLessEqual(len(detail['text'].encode('utf-8')), 4096)

    def test_preparing_records_warning_and_origin(self):
        event, rows = self.dispatch_fixture('preparing')
        self.assertEqual((event['outcome'], event['reason'], event['severity']),
                         ('failed', 'target_preparing', 'warning'))
        detail = diagnostic_trace.reassemble(rows, event['diagnostic_id'])
        self.assertEqual((detail['status'], detail['kind']), ('complete', 'origin_stack'))

    def test_dispatch_exception_remains_failure_when_logging_fails(self):
        event, rows = self.dispatch_fixture('exception', capture_fails=True)
        self.assertEqual((event['outcome'], event['reason']), ('failed', 'RuntimeError'))
        self.assertNotIn('diagnostic_id', event)
        self.assertNotIn('synthetic-dispatch-error', json.dumps(rows))
