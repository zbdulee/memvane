"""Lifecycle logs remain in fixture state even with a live HOME override."""
import os
from pathlib import Path
import pwd
from unittest import mock

from memvane.wiki.lifecycle import wiki_lifecycle as lifecycle
from tests.support.case import FixtureCase


class FixtureLogIsolation(FixtureCase):
    def test_default_and_explicit_live_log_use_runtime_root(self):
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        with mock.patch.dict(os.environ, {'HOME': str(home)}, clear=False):
            os.environ.pop('QMD_WIKI_LIFECYCLE_LOG', None)
            self.assertIn(self.runtime, lifecycle.log_path().resolve().parents)
            os.environ['QMD_WIKI_LIFECYCLE_LOG'] = str(home / 'lifecycle-fixture-sentinel')
            path = lifecycle.log_path()
            self.assertIn(self.runtime, path.resolve().parents)
            lifecycle.log('fixture lifecycle')
            self.assertIn('fixture lifecycle', path.read_text())
