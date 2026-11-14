"""External edit routing over the selected project registry.

A complete, healthy registry answers external edit target discovery
(``wiki.intake.api.notify``) without listing the installs base. An absent,
corrupt, busy or unsupported registry refuses (no fallback scan). While the
backfill is incomplete the bounded scan runs with the registry's tombstones
applied. A registry row is a candidate only: the same journal and route proof
decides. Project A edits a raw file of project B; B's sync wake is the route.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from memvane.kernel import memvane_paths
from memvane.registry import memvane_registry as registry
from memvane.registry.tests.support import RegistryCase


class Routing(RegistryCase):
    installs = 2

    def setUp(self):
        super().setUp()
        self.wakes = []
        patcher = mock.patch('memvane.runtime.api.request',
                             side_effect=lambda kind, target='', **_kw: self.wakes.append((kind, target))
                             or {'status': 'queued'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = self.b.project / 'note.md'
        self.file.write_text('B knowledge\n')
        self.scans = []

    def notify(self, path=None):
        from memvane.wiki.intake.api import notify
        self.scans.clear()
        self.wakes.clear()
        real, base = os.scandir, memvane_paths.installs_base()

        def counted(target='.'):
            if isinstance(target, (str, os.PathLike)) and Path(target) == base:
                self.scans.append(str(target))
            return real(target)
        # Only the installs base listing is counted; fd scandir stays the real function.
        with mock.patch.object(os, 'scandir', counted):
            return notify([str(path or self.file)], cwd=str(self.a.project), host='codex',
                          origin=str(self.a.project))

    def routed(self, selected=None):
        return ('laya-sync', str((selected or self.b).project)) in self.wakes


class RegistryTargets(Routing):
    def test_an_absent_registry_refuses_without_an_installs_base_scan(self):
        self.assertEqual(self.notify(), {'owners': 0, 'failed': 1, 'overflow': 0})
        self.assertFalse(self.routed())
        self.assertEqual(self.scans, [])
        self.assertFalse(self.db.exists())

    def test_a_complete_registry_routes_without_listing_the_installs_base(self):
        self.backfill()
        result = self.notify()
        self.assertEqual(result['owners'], 1, result)
        self.assertTrue(self.routed())
        self.assertEqual(self.scans, [])

    def test_corrupt_unsupported_and_busy_registries_refuse_and_are_preserved(self):
        self.backfill()
        good = self.db.read_bytes()

        def unsupported():
            self.sql("UPDATE meta SET value='memvane-project-registry-v9' WHERE key='schema'")
        for name, damage in (('corrupt', lambda: self.db.write_bytes(b'x' * 4096)), ('unsupported', unsupported)):
            with self.subTest(case=name):
                self.db.write_bytes(good)
                damage()
                before = hashlib.sha256(self.db.read_bytes()).hexdigest()
                self.assertEqual(self.notify()['failed'], 1)
                self.assertFalse(self.routed())
                self.assertEqual(self.scans, [])
                self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.db.write_bytes(good)
        holder = sqlite3.connect(str(self.db), timeout=0)
        self.addCleanup(holder.close)
        holder.execute('BEGIN EXCLUSIVE')
        self.assertEqual(self.notify()['failed'], 1)
        self.assertFalse(self.routed())
        holder.rollback()

    def test_a_registry_row_without_a_current_route_proof_does_not_route(self):
        self.backfill()
        route = self.b.project / '.memvane' / 'route.json'
        os.chmod(route, 0o600)
        route.write_bytes(b'{"schema":')
        self.assertEqual(self.notify()['owners'], 0)
        self.assertFalse(self.routed())

    def test_forged_registry_rows_grant_nothing_without_the_owner_proof(self):
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        self.backfill()
        project = make_project(self.home, name='gamma', dirname='gamma')
        project.write_note('notes.md', 'Gamma notes.\n')
        project.commit()
        pending = build_install(self, project, upto='finalized', install_id='8' * 32)
        draft = project.write_note('draft.md', 'pending\n')
        for install_id, state, root in ((pending.install_id, pending.state, project.root),  # journal unselected
                                        ('f' * 32, self.b.state, project.root),             # another root
                                        ('1' * 32, self.home / ('1' * 32), self.b.project)):  # off the route
            self.insert(install_id, state, root, root)
        self.assertEqual(self.notify(draft)['owners'], 0)
        self.assertFalse(self.routed())
        self.assertNotIn(('laya-sync', str(project.root)), self.wakes)
        result = self.notify()
        self.assertEqual(result['owners'], 1, result)  # B only, through its own proof

    def test_missing_pending_detached_and_conflict_rows_do_not_route(self):
        self.backfill()
        for status, reason in (('missing_pending', None), ('detached', 'missing'), ('conflict', None)):
            with self.subTest(status=status):
                self.sql('UPDATE entries SET status=?, reason=?, version=version+1 WHERE install_id=?',
                         (status, reason, self.b.install_id))
                self.assertEqual(self.notify()['owners'], 0)
                self.assertFalse(self.routed())

    def test_the_nested_check_is_bounded_by_depth_and_holds_deeper_edits(self):
        from memvane.wiki.intake.types import MAX_DEPTH
        self.backfill()
        shallow = self.b.fixture.write_note('a/b/note.md', 'shallow\n')
        deep = self.b.fixture.write_note('/'.join(f'd{n}' for n in range(MAX_DEPTH + 5)) + '/note.md', 'deep\n')
        counted = []
        real = os.stat

        def stat(path, *args, **kwargs):
            if str(path).endswith('settings.json'):
                counted.append(str(path))
            return real(path, *args, **kwargs)
        with mock.patch.object(os, 'stat', side_effect=stat):
            self.assertEqual(self.notify(shallow)['owners'], 1)
            self.assertLessEqual(len(counted), 2 * MAX_DEPTH)
            counted.clear()
            self.assertEqual(self.notify(deep)['owners'], 0)
            self.assertFalse(self.routed())
            self.assertLessEqual(len(counted), 2 * MAX_DEPTH)

    def test_nested_project_folders_hold_out_a_registered_owner_only(self):
        # Registry lookup lists registered owners only, so a project folder
        # between the file and the owner root holds the edit back. The scan
        # (incomplete backfill) keeps the plain owner choice.
        self.backfill()
        nested = self.b.project / 'nested'
        (nested / '.memvane').mkdir(parents=True)
        (nested / '.memvane' / 'settings.json').write_text('{"name": "nested", "skipPaths": []}\n')
        path = nested / 'note.md'
        path.write_text('nested\n')
        self.assertEqual(self.notify(path)['owners'], 0)
        self.assertFalse(self.routed())
        self.assertEqual(self.notify()['owners'], 1)  # B's own file still routes
        self.sql('UPDATE scans SET complete_at=NULL')
        self.assertEqual(self.notify(path)['owners'], 1)
        self.assertTrue(self.routed())

    def test_rows_outside_the_home_installs_base_do_not_route(self):
        self.backfill()
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        copied = elsewhere / self.b.install_id
        os.rename(self.b.state, copied)
        self.addCleanup(os.rename, copied, self.b.state)
        self.sql('UPDATE entries SET state_root=? WHERE install_id=?', (str(copied), self.b.install_id))
        self.assertEqual(self.notify()['owners'], 0)
        self.assertFalse(self.routed())

    def test_a_tombstone_is_not_bypassed_by_another_active_row(self):
        self.backfill()
        self.sql("UPDATE entries SET status='detached', reason='missing', version=version+1 WHERE install_id=?",
                 (self.b.install_id,))
        self.insert('f' * 32, self.b.state, self.b.project, self.b.project / 'alias')
        self.assertEqual(self.notify()['owners'], 0)
        self.assertFalse(self.routed())

    def test_an_incomplete_backfill_scans_with_tombstones_applied(self):
        self.backfill()
        self.sql('UPDATE scans SET complete_at=NULL')
        self.assertEqual(self.notify()['owners'], 1)
        self.assertNotEqual(self.scans, [])
        self.sql("UPDATE entries SET status='detached', reason='missing' WHERE install_id=?", (self.b.install_id,))
        self.assertEqual(self.notify()['owners'], 0)
        self.assertFalse(self.routed())

    def test_an_activation_after_the_backfill_is_found_by_a_later_one(self):
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        self.backfill()
        project = make_project(self.home, name='gamma', dirname='gamma')
        project.commit()
        late = build_install(self, project, install_id='8' * 32)
        registry.reconcile(now=99999.0, max_rows=64, seconds=60.0, backfill_page=64)
        self.assertIn(late.install_id, [row['installId'] for row in registry.routing_rows()['rows']])


if __name__ == '__main__':
    unittest.main()
