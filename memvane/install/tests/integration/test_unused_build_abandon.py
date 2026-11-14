"""Restore before activation abandons the install; every proof it rests on refuses first.

Abandon is ``restore_origin_selection`` of an install that was never selected
(F2): the journal goes ``restored``, the build pointer and build record go
(the record last) and the state root, store, journal and staged route stay.
It refuses, changing nothing, for another project's install, a changed staged
route or ledger, a ledger phase the journal contradicts, or a busy owner, and
it never touches another project's pointers. Project pointers of a lower or
unknown major are released without reading any state root, so the next plan
starts a new onboarding (R2, the D5 exit). The abandon lifecycle with crash
points is ``tests/e2e/onboarding_fixture.py``.
"""
import os
import unittest
from unittest import mock

from memvane.install import api as install
from memvane.install.application import onboarding_restore
from memvane.install.tests.support import OTHER_INSTALL, InstallCase, canonical
from tests.support.project import make_project


class Abandon(InstallCase):
    def setUp(self):
        super().setUp()
        self.target = self.install()
        self.assertEqual(self.target.prepare()['status'], 'initial_build_registered')
        self.state = self.target.state

    def journal_state(self):
        from memvane.journal.api import inspect_journal
        return inspect_journal(self.state / 'journal.sqlite')['state']

    def snapshot(self):
        """What a refused restore must leave unchanged."""
        return (self.journal_state(), sorted(name for name in os.listdir(self.state) if not name.endswith('.lock')),
                install.build_lookup(str(self.project)),
                self.pointers())

    def restore(self, project=None):
        return install.restore_origin_selection(project_root=str(project or self.project),
                                                state_root=str(self.state))

    def test_abandon_keeps_the_state_root_and_the_next_rerun_applies_nothing(self):
        self.assertEqual(self.restore(), {'status': 'restored', 'changesApplied': 1})
        self.assertEqual(self.journal_state(), 'restored')
        self.assertIsNone(install.build_lookup(str(self.project)))
        self.assertTrue({'journal.sqlite', 'store.sqlite', 'route.json'} <= set(os.listdir(self.state)))
        self.assertNotIn('build.json', os.listdir(self.state))
        self.assertEqual(self.restore(), {'status': 'restored', 'changesApplied': 0})

    def test_another_projects_restore_refuses_and_changes_nothing(self):
        other = make_project(self.home, name='other', dirname='other')
        before = self.snapshot()
        self.blocked('initial_build_project_mismatch', self.restore, other.root)
        self.assertEqual(self.snapshot(), before)

    def test_a_changed_staged_route_or_ledger_refuses(self):
        before = self.snapshot()
        route = self.state / 'route.json'
        original, mode = route.read_bytes(), os.stat(route).st_mode
        os.chmod(route, 0o600)
        route.write_bytes(original + b' ')
        self.blocked('initial_build_route_changed', self.restore)
        route.write_bytes(original)
        os.chmod(route, mode)
        self.assertEqual(self.snapshot(), before)
        ledger = self.state / 'ledger.json'
        for value in ({'schema': 'memvane-install-ledger-v1', 'epoch': 1, 'phase': 'prepared', 'identity': {}},
                      {'schema': 'memvane-install-ledger-v1', 'epoch': 0, 'phase': 'prepared',
                       'identity': install.build_lookup(str(self.project))['identity']},
                      {'schema': 'memvane-install-ledger-v1', 'epoch': 1, 'phase': 'activating',
                       'identity': install.build_lookup(str(self.project))['identity']}):
            with self.subTest(ledger=value):
                fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.write(fd, canonical(value))
                os.close(fd)
                self.blocked('initial_build_ledger_changed', self.restore)
        os.unlink(ledger)
        self.assertEqual(self.snapshot(), before)

    def test_a_ledger_phase_the_journal_contradicts_refuses(self):
        identity = install.build_lookup(str(self.project))['identity']
        before = self.snapshot()
        for phase in ('selected', 'restored'):
            with self.subTest(phase=phase):
                fd = os.open(self.state / 'ledger.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.write(fd, canonical({'schema': 'memvane-install-ledger-v1', 'epoch': 3, 'phase': phase,
                                        'identity': identity}))
                os.close(fd)
                self.blocked('restore_state_invalid', self.restore)
        os.unlink(self.state / 'ledger.json')
        self.assertEqual(self.snapshot(), before)

    def test_a_missing_record_with_an_unrestored_journal_refuses(self):
        record = self.state / 'build.json'
        saved = record.read_bytes()
        os.rename(record, self.home / 'build.json')
        self.blocked('initial_build_missing', self.restore)
        self.assertEqual(self.journal_state(), 'prepared')
        os.rename(self.home / 'build.json', record)
        self.assertEqual(record.read_bytes(), saved)

    def test_busy_owners_refuse(self):
        from memvane.runtime.api import sync_owner
        before = self.snapshot()
        with install.install_lock(self.state):
            self.blocked('install_busy', self.restore)
        with sync_owner(self.state / 'store.sqlite', wait=False) as owner:
            self.assertIsNotNone(owner)
            self.blocked('owner_busy', self.restore)
        self.assertEqual(self.snapshot(), before)

    def test_another_projects_build_and_pointers_are_never_touched(self):
        other = make_project(self.home, name='other', dirname='other')
        other.write_note('notes.md', 'Other notes.\n')
        other.commit('other')
        second = self.install(OTHER_INSTALL, fixture=other)
        self.assertEqual(second.prepare()['status'], 'initial_build_registered')
        foreign = self.write_pointer('selection.json', b'{"schema":"memvane-selection-v1"}', project=other.root)
        kept = (install.build_lookup(str(other.root)), sorted(os.listdir(second.state)))
        self.assertEqual(self.restore(), {'status': 'restored', 'changesApplied': 1})
        self.assertEqual((install.build_lookup(str(other.root)), sorted(os.listdir(second.state))), kept)
        self.assertEqual(self.pointers(other.root), {'selection.json': foreign})


class OtherMajorRelease(InstallCase):
    """R2: restore releases pointers of a lower or unknown major without any state root."""

    def release(self):
        return install.restore_origin_selection(project_root=str(self.project),
                                                state_root=str(self.home / 'no-such-state-root'))

    def removal_order(self):
        order = []
        real = onboarding_restore._retire_foreign_pointer

        def retire(path):
            order.append(path.name)
            real(path)
        return order, mock.patch.object(onboarding_restore, '_retire_foreign_pointer', retire)

    def test_pointers_of_another_major_are_released_the_mismatching_one_last(self):
        route = self.foreign_route()
        cases = (
            # An unreadable (other-major) route carries the mismatch: the marker goes first.
            ({'route.json': {**route, 'schema': 'laya-index-route-v1'}, 'selection.json': self.marker()},
             ['selection.json', 'route.json']),
            # A readable route with a lower-major marker: the marker carries it and goes last.
            ({'route.json': route, 'selection.json': self.marker(schema='memvane-selection-v0')},
             ['route.json', 'selection.json']),
            ({'route.json': {**route, 'schema': 'laya-index-route-v1'}}, ['route.json']),
        )
        for pointers, expected in cases:
            with self.subTest(expected=expected):
                for name, record in pointers.items():
                    self.write_pointer(name, record)
                order, patched = self.removal_order()
                with patched:
                    self.assertEqual(self.release(), {'status': 'released', 'changesApplied': len(expected)})
                self.assertEqual((order, self.pointers()), (expected, {}))
                self.assertEqual(install.plan_fresh_wiki_origin(self.project)['status'], 'review_required')

    def test_newer_major_pointers_are_never_released(self):
        saved = {'route.json': self.write_pointer('route.json', self.foreign_route(stateFormat=2)),
                 'selection.json': self.write_pointer('selection.json', self.marker())}
        for _ in range(2):
            self.assertIsNone(onboarding_restore._release_other_major(self.project))
        self.assertEqual(self.pointers(), saved)
        self.blocked('project_selection_already_exists', install.plan_fresh_wiki_origin, self.project)

    def test_no_mismatch_falls_through_to_the_install_restore(self):
        self.assertIsNone(onboarding_restore._release_other_major(self.project))
        self.blocked('unsafe_install_parent', self.release)


if __name__ == '__main__':
    unittest.main()
