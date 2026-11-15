import contextlib
import io
import json
import sys
from unittest.mock import patch

from entry import run as entry
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from memvane.diagnostics import diagnostic_trace


class CompileWorkerDiagnosticsTests(FixtureCase):
    def test_verify_exception_is_swallowed_and_obeys_diagnostic_consent(self):
        project = make_project(self.home, git=False)
        install = build_install(self, project)
        raw = json.loads(project.settings_path.read_text())
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                raw['diagnostics'] = {'enabled': enabled}
                project.settings_path.write_text(json.dumps(raw))
                before = len(install.events())
                argv = ['entry/run.py', 'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all']
                with patch.object(sys, 'argv', argv), \
                     patch('memvane.wiki.verify.api.run', side_effect=ValueError('synthetic-verify-error')), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(entry.main(), 0)
                rows = install.events()[before:]
                matching = [r for r in rows if r.get('event') == 'compile.verify_pass']
                self.assertEqual(len(matching), 1, rows)
                row = matching[0]
                self.assertEqual((row['outcome'], row['reason']), ('error', 'ValueError'))
                if enabled:
                    detail = diagnostic_trace.reassemble(rows, row['diagnostic_id'])
                    self.assertEqual(detail['status'], 'complete')
                    self.assertIn('synthetic-verify-error', detail['text'])
                else:
                    self.assertNotIn('diagnostic_id', row)
                    self.assertNotIn('synthetic-verify-error', json.dumps(rows))
