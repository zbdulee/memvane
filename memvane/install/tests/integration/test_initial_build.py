"""The registered initial build: its durable phase, its status reads and the rescan request.

Prepare registers the build ``registered``; sync passes move it through
``building`` to ``ready``; only a ``ready`` build finalizes. Status reads are
read only and answer for the owning project alone; a rescan moves an
unselected build back to ``building`` and requests one wake. A busy build
owner refuses every writer. (Rescan after selection and the finalize port are
``tests/e2e/onboarding_fixture.py``.)
"""
import os
import stat
import unittest

from memvane.install import api as install
from memvane.install.tests.support import OTHER_INSTALL, InstallCase


class InitialBuild(InstallCase):
    def setUp(self):
        super().setUp()
        self.target = self.install()
        self.assertEqual(self.target.prepare()['status'], 'initial_build_registered')
        self.state = self.target.state

    def status(self, **kwargs):
        return install.initial_build_status(str(self.state), **kwargs)

    def record_bytes(self):
        return (self.state / 'build.json').read_bytes()

    def test_phase_moves_registered_to_ready_and_status_only_reads_it(self):
        self.assertEqual(self.status(project_root=str(self.project)),
                         {'status': 'initial_build', 'phase': 'registered', 'reason': None, 'epoch': 1})
        before = self.record_bytes()
        inspected = install.index_origin_status(str(self.state))
        self.assertEqual((inspected['status'], inspected['journal']['state'], inspected['initialBuild']['phase']),
                         ('inspected', 'prepared', 'registered'))
        self.assertEqual(self.record_bytes(), before)
        phases = [result.get('buildPhase') for result in self.target.build()]
        self.assertEqual(phases[-1], 'ready')
        self.assertTrue(set(phases) <= {'registered', 'building', 'ready', None}, phases)
        ready = self.status()
        self.assertEqual((ready['phase'], ready['reason']), ('ready', None))
        self.assertGreater(ready['epoch'], 1)
        self.assertEqual(self.status(), ready)

    def test_finalize_before_ready_is_pending_and_writes_no_ledger(self):
        for _ in range(2):
            result = self.target.finalize()
            self.assertEqual(result, {'status': 'initial_build_pending', 'phase': 'registered'})
        self.assertFalse((self.state / 'ledger.json').exists())
        self.assertEqual(self.status()['epoch'], 1)

    def test_status_and_rescan_answer_only_for_the_owning_project(self):
        other = self.home / 'other'
        other.mkdir()
        before = self.record_bytes()
        self.blocked('initial_build_project_mismatch', self.status, project_root=str(other))
        self.blocked('initial_build_project_mismatch', install.request_initial_build_rescan,
                     str(self.state), project_root=str(other))
        self.assertEqual(self.record_bytes(), before)
        empty = self.install(OTHER_INSTALL).state
        empty.mkdir(mode=0o700)
        self.blocked('initial_build_missing', install.initial_build_status, str(empty))
        self.blocked('initial_build_missing', install.request_initial_build_rescan, str(empty))
        self.assertEqual([name for name in os.listdir(empty) if not name.endswith('.lock')], [])

    def test_rescan_moves_a_ready_unselected_build_back_to_building_with_one_wake(self):
        self.target.build()
        ready = self.status()
        result = install.request_initial_build_rescan(str(self.state), project_root=str(self.project))
        self.assertEqual((result['status'], result['phase']), ('rescan_requested', 'building'))
        self.assertIn('status', result['wake'])
        self.assertEqual(self.status(), {'status': 'initial_build', 'phase': 'building', 'reason': None,
                                         'epoch': ready['epoch'] + 1})
        self.assertEqual(self.target.build()[-1]['buildPhase'], 'ready')

    def test_a_busy_build_owner_refuses_the_writers(self):
        before = self.record_bytes()
        with install.build_owner(self.state) as owned:
            self.assertTrue(owned)
            self.blocked('initial_build_busy', install.request_initial_build_rescan, str(self.state))
            self.blocked('initial_build_busy', self.target.finalize)
            self.blocked('initial_build_busy', self.target.restore)
        self.assertEqual(self.record_bytes(), before)
        with install.install_lock(self.state):
            self.blocked('install_busy', install.request_initial_build_rescan, str(self.state))

    def test_prepare_reruns_verify_the_registration(self):
        record = self.record_bytes()
        for _ in range(2):
            self.assertEqual(self.target.prepare()['status'], 'initial_build_registered')
        self.assertEqual(self.record_bytes(), record)
        for name in ('build.json', 'route.json', 'store.sqlite', 'journal.sqlite'):
            info = (self.state / name).lstat()
            self.assertEqual((stat.S_ISREG(info.st_mode), info.st_nlink, stat.S_IMODE(info.st_mode) & 0o077),
                             (True, 1, 0), name)
        # Another install id for the same project is refused while this build is registered.
        self.blocked('initial_build_already_registered', self.install(OTHER_INSTALL).prepare)
        self.assertEqual(install.build_lookup(str(self.project))['identity']['installId'],
                         self.target.install_id)


if __name__ == '__main__':
    unittest.main()
