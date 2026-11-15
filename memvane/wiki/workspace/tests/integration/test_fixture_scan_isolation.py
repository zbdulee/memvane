"""Scan state remains in fixture state even with a live HOME override."""
import os
from pathlib import Path
import pwd
from unittest import mock

from memvane.wiki.workspace import scan_state
from tests.support.case import FixtureCase


class FixtureScanIsolation(FixtureCase):
    def test_default_and_explicit_live_state_use_runtime_root(self):
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        with mock.patch.dict(os.environ, {'HOME': str(home)}, clear=False):
            os.environ.pop('QMD_SYNC_STATE_DIR', None)
            self.assertIn(self.runtime, scan_state.state_dir().resolve().parents)
            os.environ['QMD_SYNC_STATE_DIR'] = str(home / 'scan-fixture-sentinel')
            self.assertIn(self.runtime, scan_state.state_dir().resolve().parents)
            self.assertIn(self.runtime, scan_state.state_dir(home / 'explicit').resolve().parents)
