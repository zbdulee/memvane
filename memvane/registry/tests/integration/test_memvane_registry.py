"""Selected project registry v1: a lookup index, never an authority.

One owner-only SQLite file under the HOME state base lists selected main
checkouts and their linked worktrees. Rows come from the selected-route proof
(``journal.api.select_route`` and the install journal); every consumer repeats
that proof before use, and the registry never writes journal, route, ledger,
marker or store bytes. The installs here are real 1.0 installs selected
through the install use cases in the case HOME.
"""
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from memvane.kernel import memvane_paths
from memvane.registry import memvane_registry as registry
from memvane.registry.tests.support import DAY, RegistryCase, sha

ROOT = Path(__file__).resolve().parents[4]


class Store(RegistryCase):
    def test_absent_registry_refuses_reads_and_creates_nothing(self):
        with self.assertRaisesRegex(ValueError, 'registry_absent'):
            registry.routing_rows()
        self.assertFalse(registry.registry_path().parent.exists())

    def test_register_writes_one_private_main_row_idempotently(self):
        first = self.register(self.a)
        self.assertEqual(first['status'], 'registered', first)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db.parent.stat().st_mode & 0o777, 0o700)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['kind'], row['state_root'], row['project_root']),
                         ('active', 'main', str(self.a.state), str(self.a.project)))
        self.assertEqual((row['inode'], row['incarnation']), (self.a.project.stat().st_ino, self.a.scope_id))
        again = self.register(self.a, now=2000.0)
        self.assertEqual(again['status'], 'unchanged', again)
        self.assertEqual(self.row(self.a)['version'], row['version'])
        self.assertEqual(len(self.rows()), 1)

    def test_case_aliases_of_a_root_are_one_row(self):
        alias = Path(str(self.a.project).swapcase())
        try:
            same = os.path.samefile(alias, self.a.project)
        except OSError:
            same = False
        if not same:
            self.skipTest('case-sensitive file system')
        self.assertEqual(self.register(self.a)['status'], 'registered')
        found = registry.lookup(str(alias / 'notes.md'))
        self.assertEqual((found['status'], found['row']['installId'], found['row']['targetRoot']),
                         ('owner', self.a.install_id, str(self.a.project)))
        self.assertEqual(self.register(self.a, now=2000.0)['status'], 'unchanged')
        self.assertEqual(list(self.rows()), [(self.a.install_id, str(self.a.project))])

    def test_registry_writes_no_journal_route_ledger_or_store_bytes(self):
        watched = [self.a.journal, self.a.state / 'route.json', self.a.state / 'ledger.json',
                   self.a.project / '.memvane' / 'route.json', self.a.project / '.memvane' / 'selection.json',
                   self.a.state / 'store.sqlite']
        before = {path: sha(path) for path in watched}
        self.register(self.a)
        self.backfill()
        self.assertEqual({path: sha(path) for path in watched}, before)

    def test_an_unselected_install_is_not_registered(self):
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        project = make_project(self.home, name='gamma', dirname='gamma')
        project.write_note('notes.md', 'Gamma notes.\n')
        project.commit()
        pending = build_install(self, project, upto='finalized', install_id='8' * 32)
        result = registry.register(str(project.root), str(pending.state), explicit=True, now=1000.0)
        self.assertEqual(result['status'], 'unproved', result)
        self.assertEqual(self.rows(), {})

    def test_a_state_root_or_seed_outside_the_home_base_writes_nothing(self):
        foreign = self.home / 'elsewhere' / ('1' * 32)
        foreign.mkdir(parents=True, mode=0o700)
        result = registry.register(str(self.a.project), str(foreign), explicit=True, now=1000.0)
        self.assertEqual(result['status'], 'skipped', result)
        result = registry.reconcile(seed=(str(self.a.project), str(foreign)), now=1000.0)
        self.assertEqual(result['status'], 'skipped', result)
        self.assertFalse(registry.registry_path().parent.exists())

    def test_a_fixture_process_never_touches_the_account_home(self):
        import pwd
        account = type('Account', (), {'pw_dir': str(self.home)})()
        with mock.patch.object(pwd, 'getpwuid', return_value=account):
            self.assertEqual(self.register(self.a)['status'], 'unavailable')
            self.assertEqual(registry.reconcile(now=1000.0)['status'], 'unavailable')
            with self.assertRaisesRegex(ValueError, 'registry_fixture_live_home'):
                registry.routing_rows()
        self.assertFalse(registry.registry_path().parent.exists())

    def test_a_corrupt_registry_is_refused_and_preserved(self):
        self.register(self.a)
        self.db.write_bytes(b'not a sqlite database' * 64)
        before = sha(self.db)
        with self.assertRaisesRegex(ValueError, 'registry_corrupt'):
            registry.routing_rows()
        self.assertEqual(self.register(self.b)['status'], 'unavailable')
        self.assertEqual(registry.reconcile(now=1000.0)['status'], 'unavailable')
        self.assertEqual(sha(self.db), before)
        snapshot = registry.snapshot(reconcile=False)
        self.assertEqual((snapshot['status'], snapshot['reason']), ('recovery_required', 'registry_corrupt'))

    def test_an_unsupported_schema_is_never_written(self):
        self.register(self.a)
        self.sql("UPDATE meta SET value='memvane-project-registry-v9' WHERE key='schema'")
        before = sha(self.db)
        with self.assertRaisesRegex(ValueError, 'registry_schema_unsupported'):
            registry.routing_rows()
        self.assertEqual(self.register(self.b)['status'], 'unavailable')
        self.assertEqual(sha(self.db), before)

    def test_unsafe_registry_paths_are_refused(self):
        self.register(self.a)
        self.db.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'registry_unsafe'):
            registry.routing_rows()
        self.db.chmod(0o600)
        moved = self.db.parent.with_name('registry-real')
        self.db.parent.rename(moved)
        self.db.parent.symlink_to(moved)
        with self.assertRaisesRegex(ValueError, 'registry_unsafe'):
            registry.routing_rows()
        self.assertEqual(self.register(self.b)['status'], 'unavailable')

    def test_a_busy_registry_ends_in_a_bounded_refusal(self):
        self.register(self.a)
        holder = sqlite3.connect(str(self.db), timeout=0)
        self.addCleanup(holder.close)
        holder.execute('BEGIN EXCLUSIVE')
        with self.assertRaisesRegex(ValueError, 'registry_busy'):
            registry.routing_rows(busy_ms=0)
        self.assertEqual(self.register(self.b)['status'], 'unavailable')
        holder.rollback()

    def test_lookup_uses_the_longest_component_prefix_and_rejects_ties(self):
        self.assertEqual(self.register(self.a)['status'], 'registered')
        nested = self.a.project / 'sub'
        nested.mkdir()
        self.insert('8' * 32, self.home, nested, nested)
        found = registry.lookup(str(nested / 'x' / 'y.md'))
        self.assertEqual((found['status'], found['row']['installId']), ('owner', '8' * 32))
        self.assertEqual(registry.lookup(str(self.a.project / 'subx' / 'y.md'))['row']['installId'],
                         self.a.install_id)
        self.assertEqual(registry.lookup(str(self.home / 'elsewhere.md'))['status'], 'none')
        self.insert('e' * 32, self.home, self.b.project, self.b.project)
        self.register(self.b)
        self.assertEqual(registry.lookup(str(self.b.project / 'n.md'))['status'], 'ambiguous')

    def test_concurrent_writers_lose_by_version_not_by_overwrite(self):
        self.register(self.a)
        version = self.row(self.a)['version']
        self.a.project.rename(self.a.project.with_name('alpha-away'))
        seen = []

        def race(target_root, expected):
            self.sql("UPDATE entries SET status='conflict', version=version+1 WHERE target_root=?",
                     (target_root,))
            seen.append((target_root, expected))
        with mock.patch.object(registry, '_before_store', race):
            result = registry.reconcile(now=5000.0, max_rows=8, seconds=60.0, backfill_page=0)
        self.assertEqual((seen, result['raced']), ([(str(self.a.project), version)], 1))
        row = self.row(self.a)
        self.assertEqual((row['status'], row['version']), ('conflict', version + 1))

    def test_a_registration_racing_a_concurrent_change_writes_nothing(self):
        self.register(self.a)
        seen = []

        def race(target_root, expected):
            self.insert('e' * 32, self.home, target_root, target_root)
            seen.append((target_root, expected))
        with mock.patch.object(registry, '_before_store', race):
            result = self.register(self.b)
        self.assertEqual((result['status'], seen), ('raced', [(str(self.b.project), None)]))
        self.assertIsNone(self.row(self.b))
        self.assertEqual({iid: row['status'] for (iid, root), row in self.rows().items()
                          if root == str(self.b.project)}, {'e' * 32: 'active'})

    def test_a_release_racing_a_concurrent_reregistration_keeps_the_row(self):
        self.register(self.a)
        self.assertEqual(self.a.install.restore()['status'], 'restored')
        version = self.row(self.a)['version']

        def race(target_root, expected):
            self.sql('UPDATE entries SET observed_at=7, version=version+1 WHERE target_root=?', (target_root,))
        with mock.patch.object(registry, '_before_store', race):
            result = registry.release(str(self.a.project), str(self.a.state), now=2000.0)
        self.assertEqual(result['status'], 'raced', result)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['version']), ('active', version + 1))
        self.assertEqual(registry.release(str(self.a.project), str(self.a.state), now=3000.0)['status'],
                         'released')

    def test_a_registry_written_by_this_python_is_readable_by_the_other(self):
        self.register(self.a)
        other = '/usr/bin/python3' if sys.version_info >= (3, 10) else shutil.which('python3.13')
        if not other or not Path(other).exists():
            self.skipTest('second python unavailable')
        code = ('import json; from memvane.registry import api;'
                'print(json.dumps(sorted(x["installId"] for x in api.routing_rows()["rows"])))')
        out = subprocess.run([other, '-B', '-c', code], cwd=str(ROOT), text=True, capture_output=True,
                             timeout=60, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [self.a.install_id])


class Lifecycle(RegistryCase):
    installs = 2

    """``after_setup``: register on a selected activation, detach only on a proved release."""

    def after(self, action, selected, result):
        return registry.after_setup(action, str(selected.project), str(selected.state), result)

    def test_a_proved_release_detaches_main_and_worktree_rows(self):
        self.register(self.a)
        self.insert(self.a.install_id, self.a.state, self.a.project, self.home / 'alpha-wt', kind='worktree',
                    parent=self.a.install_id)
        result = self.a.install.restore()
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(self.after('restore-index-origin', self.a, result)['status'], 'released')
        for target in (self.a.project, self.home / 'alpha-wt'):
            row = self.row(self.a, target)
            self.assertEqual((row['status'], row['reason']), ('detached', 'released'), target)

    def test_failed_restores_or_an_unreleased_selection_keep_the_row_active(self):
        self.register(self.a)
        for result in ({'status': 'rejected', 'reason': 'rollback_forward_only', 'changesApplied': 0},
                       {'status': 'blocked', 'reason': 'x'}, {'status': 'recovery_required'},
                       {'status': 'restored', 'changesApplied': 1}):
            with self.subTest(result=result):
                self.after('restore-index-origin', self.a, result)
                self.assertEqual(self.row(self.a)['status'], 'active')

    def test_activation_registers_only_a_proved_selected_route(self):
        self.assertEqual(self.after('activate-index-origin', self.a, {'status': 'selected', 'phase': 'selected'})
                         ['status'], 'registered')
        self.assertEqual(self.row(self.a)['status'], 'active')
        for result in ({'status': 'initial_build_finalized', 'phase': 'prepared'},
                       {'status': 'rejected', 'phase': 'selected'}):
            with self.subTest(result=result), mock.patch.object(registry, 'register') as register:
                self.assertIsNone(self.after('activate-index-origin', self.b, result))
                register.assert_not_called()
        # A finalized but unselected install never becomes a row, whatever the result says.
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        project = make_project(self.home, name='gamma', dirname='gamma')
        project.commit()
        pending = build_install(self, project, upto='finalized', install_id='8' * 32)
        self.assertEqual(registry.after_setup('activate-index-origin', str(project.root), str(pending.state),
                                              {'status': 'selected', 'phase': 'selected'})['status'], 'unproved')

    def test_a_registry_failure_never_raises_out_of_after_setup(self):
        with mock.patch.object(registry, 'register', side_effect=RuntimeError('boom')):
            self.assertIsNone(self.after('activate-index-origin', self.a, {'phase': 'selected'}))

    def test_explicit_activation_replaces_a_missing_tombstone_backfill_does_not(self):
        self.register(self.a)
        self.sql("UPDATE entries SET status='detached', reason='missing', version=version+1")
        self.backfill()
        self.assertEqual(self.row(self.a)['status'], 'detached')
        self.assertEqual(self.register(self.a, explicit=False)['status'], 'tombstoned')
        self.assertEqual(self.row(self.a)['status'], 'detached')
        self.assertEqual(self.register(self.a, explicit=True)['status'], 'registered')
        self.assertEqual((self.row(self.a)['status'], self.row(self.a)['reason']), ('active', None))


class Reconcile(RegistryCase):
    def away(self, selected, name='away'):
        moved = selected.project.with_name(selected.project.name + '-' + name)
        selected.project.rename(moved)
        return moved

    def replace_inode(self, selected):
        moved = selected.project.with_name(selected.project.name + '-old')
        shutil.copytree(selected.project, moved, symlinks=True)
        shutil.rmtree(selected.project)
        moved.rename(selected.project)

    def test_a_missing_root_is_pending_at_once_and_detached_after_two_observations_and_a_day(self):
        self.register(self.a)
        away = self.away(self.a)
        registry.reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen']), ('missing_pending', 1))
        self.assertEqual(registry.lookup(str(self.a.project / 'x.md'))['status'], 'none')
        registry.reconcile(now=2010.0, backfill_page=0)  # same burst: not independent
        self.assertEqual(self.row(self.a)['missing_seen'], 1)
        registry.reconcile(now=2000.0 + 3600, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'missing_pending')
        registry.reconcile(now=2000.0 + DAY + 1, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['reason']), ('detached', 'missing'))
        away.rename(self.a.project)
        registry.reconcile(now=2000.0 + DAY + 3600, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'detached')

    def test_a_day_alone_or_two_observations_alone_do_not_detach(self):
        self.register(self.a)
        self.away(self.a)
        registry.reconcile(now=2000.0, backfill_page=0)
        registry.reconcile(now=2000.0 + DAY + 5, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'detached')  # two observations and a day
        self.register(self.b)
        self.away(self.b)
        for step in range(5):
            registry.reconcile(now=10000.0 + step * 120, backfill_page=0)
        self.assertEqual(self.row(self.b)['status'], 'missing_pending')

    def test_an_emptied_registry_is_never_reinitialized_or_backfilled(self):
        self.register(self.a)
        self.db.write_bytes(b'')
        result = registry.reconcile(now=2000.0, max_rows=64, seconds=60.0, backfill_page=64)
        self.assertEqual((result['status'], result.get('reason')), ('unavailable', 'registry_corrupt'), result)
        self.assertEqual(self.register(self.a)['status'], 'unavailable')
        self.assertEqual(self.db.read_bytes(), b'')
        with self.assertRaisesRegex(ValueError, 'registry_corrupt'):
            registry.routing_rows()

    def test_implicit_registration_never_adopts_a_replaced_root(self):
        self.register(self.a)
        inode = self.row(self.a)['inode']
        self.replace_inode(self.a)
        self.assertNotEqual(self.a.project.stat().st_ino, inode)
        # Backfill reaches the install before any row check does.
        registry.reconcile(now=2000.0, max_rows=0, seconds=60.0, backfill_page=64)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['reason'], row['inode']), ('detached', 'root_moved', inode))
        self.assertEqual(self.register(self.a, explicit=False, now=3000.0)['status'], 'tombstoned')
        self.assertEqual(self.row(self.a)['status'], 'detached')

    def missing_twice_then_back(self):
        self.register(self.a)
        away = self.away(self.a)
        registry.reconcile(now=2000.0, backfill_page=0)
        registry.reconcile(now=2000.0 + 3600, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen']), ('missing_pending', 2))
        away.rename(self.a.project)  # the original inode returns after the grace

    def test_a_root_back_after_the_grace_is_not_reactivated_by_reconcile(self):
        self.missing_twice_then_back()
        registry.reconcile(now=2000.0 + DAY + 10, backfill_page=0)
        row = self.row(self.a)
        self.assertNotEqual(row['status'], 'active')
        self.assertEqual(row['missing_since'], 2000.0)

    def test_a_root_back_after_the_grace_is_not_reactivated_by_implicit_registration(self):
        self.missing_twice_then_back()
        registry.reconcile(now=2000.0 + DAY + 10, max_rows=0, seconds=60.0, backfill_page=64)
        self.register(self.a, explicit=False, now=2000.0 + DAY + 20)
        row = self.row(self.a)
        self.assertNotEqual(row['status'], 'active')
        self.assertEqual(row['missing_since'], 2000.0)
        self.assertEqual(self.register(self.a, explicit=True, now=2000.0 + DAY + 30)['status'], 'registered')
        self.assertEqual(self.row(self.a)['status'], 'active')

    def test_one_sighting_and_a_day_neither_reactivates_nor_resets(self):
        self.register(self.a)
        away = self.away(self.a)
        registry.reconcile(now=2000.0, backfill_page=0)
        away.rename(self.a.project)
        registry.reconcile(now=2000.0 + DAY + 10, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_since'], row['missing_seen']),
                         ('missing_pending', 2000.0, 1))

    def test_a_return_within_the_grace_restores_active_an_inode_change_detaches(self):
        self.register(self.a)
        self.register(self.b)
        away = self.away(self.a)
        registry.reconcile(now=2000.0, backfill_page=0)
        away.rename(self.a.project)
        registry.reconcile(now=2000.0 + 600, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen'], row['missing_since']), ('active', 0, None))
        self.replace_inode(self.b)
        registry.reconcile(now=3000.0, backfill_page=0)
        row = self.row(self.b)
        self.assertEqual((row['status'], row['reason']), ('detached', 'root_moved'))

    def test_an_unobservable_root_is_not_counted_as_missing(self):
        self.register(self.a)
        real_stat, target = os.stat, str(self.a.project)

        def denied(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError(13, 'denied', target)
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(os, 'stat', denied):
            registry.reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen']), ('active', 0))

    def test_a_future_missing_time_neither_sticks_nor_detaches_at_once(self):
        self.register(self.a)
        self.away(self.a)
        self.sql("UPDATE entries SET status='missing_pending', missing_since=?, missing_seen=5, missing_last=?, "
                 'version=version+1', (1e12, 1e12))
        registry.reconcile(now=5000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_since'], row['missing_seen']), ('missing_pending', 5000.0, 1))
        registry.reconcile(now=5000.0 + DAY + 1, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'detached')

    def test_reconcile_never_recovers_another_install_journal(self):
        self.register(self.b)
        # A crashed writer leaves the journal with a hot rollback file.
        script = ('import os, sqlite3, sys; db = sqlite3.connect(sys.argv[1], isolation_level=None); '
                  "db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA cache_size=1'); "
                  "db.execute('BEGIN IMMEDIATE'); db.execute('CREATE TABLE junk(x)'); "
                  "[db.execute('INSERT INTO junk VALUES(randomblob(4000))') for _ in range(300)]; os._exit(9)")
        done = subprocess.run([sys.executable, '-B', '-c', script, str(self.b.journal)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 9, done.stderr)
        side = Path(str(self.b.journal) + '-journal')
        if not side.exists():
            self.skipTest('the journal does not use a rollback journal')
        before = (sha(self.b.journal), sha(side))
        self.backfill(now=2000.0)
        registry.reconcile(now=3000.0, backfill_page=0)
        self.assertTrue(side.exists(), 'the registry rolled back another install journal')
        self.assertEqual((sha(self.b.journal), sha(side)), before)

    def test_a_release_outside_setup_is_observed_as_released(self):
        from memvane.journal.api import select_route
        self.register(self.a)
        self.assertEqual(self.a.install.restore()['status'], 'restored')
        self.assertEqual(select_route(str(self.a.project), action='index')['state'], 'legacy')
        registry.reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['reason']), ('detached', 'released'))

    def test_an_unproved_route_marks_the_proof_but_never_detaches(self):
        from memvane.journal.api import select_route
        self.register(self.a)
        route = self.a.project / '.memvane' / 'route.json'
        os.chmod(route, 0o600)
        route.write_bytes(b'{"schema":')
        self.assertEqual(select_route(str(self.a.project), action='index')['state'], 'blocked')
        registry.reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual(row['status'], 'active')
        self.assertTrue(row['proof'].startswith('unproved'), row['proof'])

    def test_the_time_budget_stops_by_the_injected_clock(self):
        for selected in (self.a, self.b):
            self.register(selected)
        ticks = iter(range(0, 1000))
        result = registry.reconcile(now=2000.0, max_rows=32, seconds=1.5, clock=lambda: float(next(ticks)),
                                    backfill_page=0)
        self.assertEqual(result['checked'], 1, result)

    def test_one_failing_row_does_not_block_the_next(self):
        from memvane.journal import api as journal
        for selected in (self.a, self.b):
            self.register(selected)
        real = journal.select_route

        def flaky(cwd, *args, **kwargs):
            if cwd == str(self.a.project):
                raise RuntimeError('boom')
            return real(cwd, *args, **kwargs)
        self.away(self.b)
        with mock.patch('memvane.journal.api.select_route', side_effect=flaky):
            result = registry.reconcile(now=2000.0, backfill_page=0)
        self.assertEqual(result['checked'], 2, result)
        self.assertEqual(self.row(self.b)['status'], 'missing_pending')

    def test_backfill_pages_the_base_and_completes_only_after_a_full_enumeration(self):
        self.b  # both installs exist before the backfill
        result = registry.reconcile(now=1000.0, max_rows=0, backfill_page=1)
        self.assertFalse(result['complete'], result)
        self.assertFalse(registry.routing_rows()['complete'])
        self.assertTrue(self.backfill(now=1001.0)['complete'])
        rows = registry.routing_rows()
        self.assertTrue(rows['complete'])
        self.assertEqual(sorted(row['installId'] for row in rows['rows']),
                         sorted((self.a.install_id, self.b.install_id)))

    def test_an_unreadable_base_never_completes(self):
        base = memvane_paths.installs_base()
        base.chmod(0)
        self.addCleanup(base.chmod, 0o700)
        for step in range(4):
            result = registry.reconcile(now=1000.0 + step, max_rows=8, backfill_page=64)
        self.assertFalse(result['complete'], result)
        base.chmod(0o700)
        self.assertFalse(registry.routing_rows()['complete'])

    def test_the_state_root_follows_the_route_proof_of_the_same_install(self):
        self.register(self.a)
        self.sql('UPDATE entries SET state_root=? WHERE install_id=?',
                 (str(self.home / 'unrelated-state' / self.a.install_id), self.a.install_id))
        registry.reconcile(now=2000.0, backfill_page=0)
        self.assertEqual(self.row(self.a)['state_root'], str(self.a.state))

    def test_backfill_never_resurrects_a_tombstone_or_overwrites_another_install(self):
        self.register(self.a)
        self.sql("UPDATE entries SET status='detached', reason='missing'")
        self.insert('e' * 32, self.home / 'gone', self.b.project, self.b.project)
        self.backfill()
        self.assertEqual(self.row(self.a)['status'], 'detached')
        self.assertEqual({row['install_id']: row['status'] for row in self.rows().values()
                          if row['target_root'] == str(self.b.project)},
                         {'e' * 32: 'conflict', self.b.install_id: 'conflict'})
        self.assertEqual(registry.lookup(str(self.b.project / 'n.md'))['status'], 'ambiguous')
        self.assertNotIn(self.b.install_id, [row['installId'] for row in registry.routing_rows()['rows']])

    def test_linked_worktrees_register_with_main_and_detach_after_prune(self):
        from memvane.journal.api import select_route
        worktree = self.b.fixture.linked_worktree('wt', 'side')
        self.assertEqual(self.register(self.b)['status'], 'registered')
        row = self.row(self.b, worktree.root)
        self.assertEqual((row['kind'], row['status'], row['parent_install']), ('worktree', 'active', self.b.install_id))
        self.assertEqual(registry.lookup(str(worktree.root / 'notes.md'))['row']['kind'], 'worktree')
        self.assertNotIn(str(worktree.root), [r['targetRoot'] for r in registry.routing_rows()['rows']])
        with mock.patch('memvane.kernel.paths.run_git', side_effect=RuntimeError('git_call_failed')):
            registry.reconcile(now=2000.0, backfill_page=0)
        self.assertEqual(self.row(self.b, worktree.root)['status'], 'active')
        calls = []
        with mock.patch('memvane.journal.api.select_route',
                        side_effect=lambda cwd, *a, **k: calls.append(cwd) or select_route(cwd, *a, **k)):
            import time
            deadline = time.monotonic() + 60
            self.assertIsNone(registry._linked(self.b.project, deadline, clock=lambda: deadline + 1))
            self.assertIsNone(registry._linked(self.b.project, time.monotonic() - 1))
            registry.reconcile(now=2500.0, backfill_page=0, seconds=0.0)
        self.assertEqual(calls, [])
        self.assertEqual(self.row(self.b, worktree.root)['status'], 'active')
        self.b.fixture.git('worktree', 'remove', '--force', str(worktree.root))
        registry.reconcile(now=3000.0, backfill_page=0)
        row = self.row(self.b, worktree.root)
        self.assertEqual((row['status'], row['reason']), ('detached', 'released'))
        self.assertEqual(self.row(self.b)['status'], 'active')


class RowBudget(RegistryCase):
    installs = 5

    def test_the_row_budget_and_cursor_cover_every_row_across_runs(self):
        for selected in self.selected:
            self.register(selected)
        checked = [registry.reconcile(now=2000.0 + step, max_rows=2, seconds=60.0, backfill_page=0)['checked']
                   for step in range(3)]
        self.assertEqual(checked, [2, 2, 2])
        observed = {row['install_id'] for row in self.rows().values() if row['observed_at'] >= 2000.0}
        self.assertEqual(len(observed), 5)


class SessionStart(RegistryCase):
    """The selected SessionStart hook reconciles opportunistically and stays bounded."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch('memvane.runtime.api.request', return_value={'status': 'queued'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def session_start(self, selected):
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        event = {'cwd': str(selected.project), 'hook_event_name': 'SessionStart', 'session_id': 'test-session',
                 'source': 'startup'}
        decision = select_route(str(selected.project), action='update-queued')
        return serve_selected('update-queued', 'claude', decision, event)

    def test_session_start_registers_the_selected_project_and_backfills(self):
        self.b  # the backfill finds the other install
        self.session_start(self.a)
        self.assertEqual(self.row(self.a)['status'], 'active')
        for _ in range(4):
            self.session_start(self.a)
        self.assertEqual(self.row(self.b)['status'], 'active')

    def test_session_start_is_bounded_by_rows_and_runs_no_worktree_discovery(self):
        calls = []
        real = registry.reconcile
        with mock.patch('memvane.registry.api.reconcile', side_effect=lambda **kw: calls.append(kw) or real(**kw)):
            self.session_start(self.a)
        (call,) = calls
        self.assertLessEqual(call['max_rows'], registry.HOOK_ROWS)
        self.assertLessEqual(call['backfill_page'], registry.HOOK_BACKFILL_PAGE)
        self.assertLessEqual(call['seconds'], registry.HOOK_SECONDS)
        self.assertLessEqual(call['busy_ms'], registry.HOOK_BUSY_MS)
        self.assertIs(call['worktrees'], False)

    def test_session_start_response_is_unchanged_by_registry_state(self):
        baseline = self.session_start(self.a)
        self.assertIn('selected source update queued', baseline['hookSpecificOutput']['additionalContext'])
        directory = registry.registry_path().parent
        path = registry.registry_path()
        for case in ('corrupt', 'busy', 'unsafe'):
            with self.subTest(case=case):
                if directory.is_symlink():
                    directory.unlink()
                elif directory.exists():
                    shutil.rmtree(directory)
                holder = None
                if case == 'unsafe':
                    real = directory.with_name('registry-real')
                    real.mkdir(mode=0o700, exist_ok=True)
                    directory.symlink_to(real)
                else:
                    directory.mkdir(mode=0o700)
                    if case == 'corrupt':
                        path.write_bytes(b'not a database' * 64)
                        path.chmod(0o600)
                    else:
                        holder = sqlite3.connect(str(path), timeout=0)
                        holder.execute('CREATE TABLE x(y)')
                        holder.commit()
                        path.chmod(0o600)
                        holder.execute('BEGIN EXCLUSIVE')
                try:
                    self.assertEqual(self.session_start(self.a), baseline)
                finally:
                    if holder is not None:
                        holder.rollback()
                        holder.close()
                if case == 'corrupt':
                    self.assertEqual(path.read_bytes(), b'not a database' * 64)

    def test_session_start_reconciles_only_inside_the_hook_budget(self):
        from memvane.kernel import hook_budget
        baseline = self.session_start(self.a)
        calls = []
        with mock.patch('memvane.registry.api.reconcile', side_effect=lambda **kw: calls.append(kw) or {}):
            with mock.patch.object(hook_budget, 'remaining', return_value=0.6):
                self.assertEqual(self.session_start(self.a), baseline)
            self.assertEqual(calls, [])
            with mock.patch.object(hook_budget, 'remaining', return_value=1.2):
                self.assertEqual(self.session_start(self.a), baseline)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0]['seconds'], 0.2 + 1e-9)

    def test_session_start_with_an_exhausted_budget_or_a_failing_registry_keeps_the_response(self):
        from memvane.kernel import hook_budget
        baseline = self.session_start(self.a)
        with mock.patch.object(hook_budget, 'remaining', side_effect=hook_budget.HookDeadlineExceeded()):
            self.assertEqual(self.session_start(self.a), baseline)
        with mock.patch('memvane.registry.api.reconcile', side_effect=RuntimeError('boom')):
            self.assertEqual(self.session_start(self.a), baseline)

    def test_session_start_reconcile_runs_no_git(self):
        worktree = self.b.fixture.linked_worktree('wt', 'side')
        calls, results = [], []
        real_run, real = subprocess.run, registry.reconcile

        def spy_run(args, *rest, **kwargs):
            if isinstance(args, (list, tuple)) and args and str(args[0]).endswith('git'):
                calls.append(list(args))
            return real_run(args, *rest, **kwargs)

        def spied(**kwargs):
            with mock.patch.object(subprocess, 'run', spy_run):
                results.append(real(**kwargs))
                return results[-1]
        with mock.patch('memvane.registry.api.reconcile', side_effect=spied):
            self.session_start(self.b)
            self.session_start(self.b)  # the second pass reconciles the main row the first seeded
        self.assertEqual(calls, [], 'the SessionStart reconcile ran git')
        self.assertEqual([r['status'] for r in results], ['ok', 'ok'])
        self.assertEqual(self.row(self.b)['status'], 'active')
        self.assertIsNone(self.row(self.b, worktree.root))

    def test_hook_reconcile_journal_waits_are_bounded_by_its_budget(self):
        timeouts = []
        real_connect, real = sqlite3.connect, registry.reconcile

        def connect(target, *args, **kwargs):
            if str(target).split('?')[0].endswith('journal.sqlite'):
                timeouts.append(kwargs.get('timeout', 5.0))
            return real_connect(target, *args, **kwargs)

        def spied(**kwargs):
            with mock.patch.object(sqlite3, 'connect', connect):
                return real(**kwargs)
        with mock.patch('memvane.registry.api.reconcile', side_effect=spied):
            self.session_start(self.a)
        self.assertNotEqual(timeouts, [])
        self.assertLessEqual(max(timeouts), registry.HOOK_SECONDS)


if __name__ == '__main__':
    unittest.main()
