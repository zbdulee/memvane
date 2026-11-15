"""Runtime setup actions preserve approval and missing-runtime refusals."""
import json
import os
from pathlib import Path
import subprocess
import sys

from tests.support.case import FixtureCase


class RuntimeSetupEntry(FixtureCase):
    def test_runtime_actions_require_approval_and_report_missing(self):
        entry = Path(__file__).resolve().parents[4] / 'entry' / 'run.py'
        for action, reason in (
                ('provision-index-runtime', 'laya_embed_runtime_download_approval_required'),
                ('gc-index-runtime', 'laya_embed_runtime_missing')):
            with self.subTest(action=action):
                result = subprocess.run([sys.executable, '-B', str(entry), 'setup', action],
                                        capture_output=True, text=True, timeout=30,
                                        env={**os.environ,
                                             'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1'})
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout)['reason'], reason)
