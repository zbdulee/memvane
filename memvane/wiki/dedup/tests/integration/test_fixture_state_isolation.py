"""Wiki background state cannot escape into the account home in fixture runs."""
import os
from pathlib import Path
import pwd
from unittest import mock

from memvane.wiki.dedup import wiki_dedup_scan as dedup
from tests.support.case import FixtureCase


class FixtureStateIsolation(FixtureCase):
    def test_runtime_root_overrides_real_home_for_default_logs_and_scan_state(self):
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        with mock.patch.dict(os.environ, {'HOME': str(real_home)}, clear=False):
            for name in ('QMD_WIKI_DEDUP_LOG', 'QMD_WIKI_LIFECYCLE_LOG', 'QMD_SYNC_STATE_DIR'):
                os.environ.pop(name, None)
            paths = (dedup.log_path(),)
            for path in paths:
                self.assertIn(self.runtime, path.resolve().parents)
            dedup.log('fixture dedup')
            self.assertIn('fixture dedup', paths[0].read_text())

    def test_explicit_live_overrides_cannot_escape_fixture(self):
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        env = {'HOME': str(real_home),
               'QMD_WIKI_DEDUP_LOG': str(real_home / 'dedup-fixture-sentinel'),
               'QMD_WIKI_LIFECYCLE_LOG': str(real_home / 'lifecycle-fixture-sentinel'),
               'QMD_SYNC_STATE_DIR': str(real_home / 'scan-fixture-sentinel')}
        with mock.patch.dict(os.environ, env, clear=False):
            for path in (dedup.log_path(),):
                self.assertIn(self.runtime, path.resolve().parents)

    def test_live_home_through_symlink_and_missing_runtime_are_rejected(self):
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        alias = self.tmp / 'account-alias'
        alias.symlink_to(real_home, target_is_directory=True)
        with mock.patch.dict(os.environ, {'HOME': str(alias), 'QMD_WIKI_DEDUP_LOG': str(alias / 'sentinel')}):
            self.assertIn(self.runtime, dedup.log_path().resolve().parents)
            os.environ.pop('MEMVANE_TEST_RUNTIME_ROOT')
            with self.assertRaisesRegex(OSError, 'fixture_home_unsafe'):
                dedup.log_path()

    def test_explicit_temporary_paths_remain_available(self):
        target = self.tmp / 'dedup.log'
        with mock.patch.dict(os.environ, {'QMD_WIKI_DEDUP_LOG': str(target)}):
            dedup.log('temporary')
        self.assertIn('temporary', target.read_text())

    def test_temporary_home_alone_rejects_a_live_log_override(self):
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        with mock.patch.dict(os.environ, {'QMD_WIKI_DEDUP_LOG': str(real_home / 'sentinel')}):
            os.environ.pop('MEMVANE_TEST_RUNTIME_ROOT')
            os.environ.pop('QMD_SETUP_GUARD_FIXTURE')
            path = dedup.log_path()
            self.assertIn(self.home, path.resolve().parents)
