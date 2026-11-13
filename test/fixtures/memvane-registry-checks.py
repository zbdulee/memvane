"""Selected project registry v1: a lookup index, never an authority.

One owner-only SQLite file under the HOME state base
(``<state base>/registry/projects.sqlite``) lists selected main checkouts and
their linked worktrees. Rows come from the existing selected-route proof
(``laya_index_host_route.select_route`` + the install journal); every
consumer repeats that proof before use. Journal, route, ledger, marker and
store bytes are never written by the registry.

Every project, journal and state write stays under a temporary HOME. Real
restore runs through ``setup_entry`` with only the store capacity proof
(``laya_fresh_wiki_origin._proof``) replaced: the synthetic stores are empty.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

FIXTURES = Path(__file__).resolve().parent
REPO = FIXTURES.parents[1]
sys.path.insert(0, str(REPO / 'core'))
sys.path.insert(1, str(FIXTURES))
import laya_host_response as response  # noqa: E402
import laya_index_host_route as routes  # noqa: E402
import laya_install_journal as journal  # noqa: E402
import memvane_paths  # noqa: E402
import runtime_work  # noqa: E402
import setup_entry  # noqa: E402
import out_of_project_installs as installs  # noqa: E402

try:
    import memvane_registry
except ImportError:
    memvane_registry = None

LOADED_ROOT = str(Path(response.__file__).resolve().parents[1])
DAY = 24 * 3600


def registry():
    if memvane_registry is None:
        raise AssertionError('core/memvane_registry.py is missing')
    return memvane_registry


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='registry-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        env = patch.dict(os.environ, {
            'HOME': str(self.home), 'QMD_SANDBOX': '1', 'QMD_SETUP_GUARD_FIXTURE': '1',
            'MEMVANE_TEST_RUNTIME_ROOT': str(self.home / 'runtime-root'),
            'GIT_CONFIG_NOSYSTEM': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.work = self.home / 'work'
        self.a = installs.make_install(self.home, self.work / 'alpha', 'alpha', '3' * 32, '6' * 32)
        self.b = installs.make_install(self.home, self.work / 'beta', 'beta', '5' * 32, '7' * 32)
        self.bundles = installs.BundleReader([self.a, self.b])
        self.patch(journal, 'inspect_full_plugin_candidate', side_effect=self.bundles)

    def patch(self, obj, name, **kw):
        patcher = patch.object(obj, name, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    @property
    def db(self):
        return registry().registry_path()

    def rows(self):
        if not self.db.exists():
            return {}
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.row_factory = sqlite3.Row
            return {(row['install_id'], row['target_root']): dict(row) for row in
                    db.execute('SELECT * FROM entries ORDER BY id')}

    def row(self, install, target=None):
        target = str(target or install.project)
        found = [row for (iid, root), row in self.rows().items()
                 if iid == install.install_id and root == target]
        return found[0] if found else None

    def register(self, install, explicit=True, now=1000.0):
        return registry().register(str(install.project), str(install.state),
                                   explicit=explicit, now=now)

    def backfill(self, now=1000.0):
        result = None
        for _ in range(8):
            result = registry().reconcile(now=now, max_rows=64, seconds=60.0,
                                          backfill_page=64)
            if result['complete']:
                return result
        self.fail(f'backfill did not complete: {result}')

    def setup(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('laya_fresh_wiki_origin._proof'):
            code = setup_entry.main(list(argv))
        return code, json.loads(out.getvalue())


class Store(Base):
    def test_absent_registry_refuses_reads_and_creates_nothing(self):
        with self.assertRaisesRegex(ValueError, 'registry_absent'):
            registry().routing_rows()
        self.assertFalse(registry().registry_path().parent.exists())

    def test_register_writes_one_private_main_row_idempotently(self):
        first = self.register(self.a)
        self.assertEqual(first['status'], 'registered', first)
        info = self.db.stat()
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(self.db.parent.stat().st_mode & 0o777, 0o700)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['kind'], row['state_root'], row['project_root']),
                         ('active', 'main', str(self.a.state), str(self.a.project)))
        self.assertEqual(row['inode'], self.a.project.stat().st_ino)
        self.assertEqual(row['incarnation'], self.a.scope_id)
        again = self.register(self.a, now=2000.0)
        self.assertEqual(again['status'], 'unchanged', again)
        self.assertEqual(self.row(self.a)['version'], row['version'])

    def test_registry_writes_no_journal_route_ledger_or_store_bytes(self):
        watched = [self.a.journal, self.a.state / 'reviewed-route.json',
                   self.a.folder / routes.RECORD, self.a.state / installs.fresh.LEDGER,
                   self.a.folder / installs.fresh.MARKER, self.a.db, self.a.queue]
        before = {path: sha(path) for path in watched}
        self.register(self.a)
        self.backfill()
        self.assertEqual({path: sha(path) for path in watched}, before)

    def test_unselected_install_is_not_registered(self):
        pending = installs.make_install(self.home, self.work / 'gamma', 'gamma',
                                        '8' * 32, '9' * 32, activate=False)
        result = registry().register(str(pending.project), str(pending.state),
                                     explicit=True, now=1000.0)
        self.assertEqual(result['status'], 'unproved', result)
        self.assertIsNone(self.row(pending))

    def test_state_root_outside_home_base_writes_nothing(self):
        other = tempfile.TemporaryDirectory(prefix='registry-foreign-')
        self.addCleanup(other.cleanup)
        foreign = Path(other.name).resolve() / ('1' * 32)
        foreign.mkdir(mode=0o700)
        result = registry().register(str(self.a.project), str(foreign), explicit=True,
                                     now=1000.0)
        self.assertEqual(result['status'], 'skipped', result)
        self.assertFalse(registry().registry_path().parent.exists())

    def test_seed_outside_home_skips_the_whole_pass(self):
        other = tempfile.TemporaryDirectory(prefix='registry-foreign-')
        self.addCleanup(other.cleanup)
        foreign = Path(other.name).resolve() / ('1' * 32)
        foreign.mkdir(mode=0o700)
        result = registry().reconcile(seed=(str(self.a.project), str(foreign)), now=1000.0)
        self.assertEqual(result['status'], 'skipped', result)
        self.assertFalse(registry().registry_path().parent.exists())

    def test_fixture_process_never_touches_the_account_home(self):
        import pwd
        account = type('Account', (), {'pw_dir': str(self.home)})()
        with patch.object(pwd, 'getpwuid', return_value=account):
            self.assertEqual(self.register(self.a)['status'], 'unavailable')
            self.assertEqual(registry().reconcile(now=1000.0)['status'], 'unavailable')
            with self.assertRaisesRegex(ValueError, 'registry_fixture_live_home'):
                registry().routing_rows()
        self.assertFalse(registry().registry_path().parent.exists())

    def test_corrupt_registry_is_refused_and_preserved(self):
        self.register(self.a)
        self.db.write_bytes(b'not a sqlite database' * 64)
        before = sha(self.db)
        with self.assertRaisesRegex(ValueError, 'registry_corrupt'):
            registry().routing_rows()
        self.assertEqual(self.register(self.b)['status'], 'unavailable')
        self.assertEqual(registry().reconcile(now=1000.0)['status'], 'unavailable')
        self.assertEqual(sha(self.db), before)
        snapshot = registry().snapshot(reconcile=False)
        self.assertEqual((snapshot['status'], snapshot['reason']),
                         ('recovery_required', 'registry_corrupt'))

    def test_unsupported_schema_is_never_written(self):
        self.register(self.a)
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("UPDATE meta SET value='memvane-project-registry-v9' WHERE key='schema'")
            db.commit()
        before = sha(self.db)
        with self.assertRaisesRegex(ValueError, 'registry_schema_unsupported'):
            registry().routing_rows()
        self.assertEqual(self.register(self.b)['status'], 'unavailable')
        self.assertEqual(sha(self.db), before)

    def test_unsafe_registry_paths_are_refused(self):
        self.register(self.a)
        self.db.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'registry_unsafe'):
            registry().routing_rows()
        self.db.chmod(0o600)
        moved = self.db.parent.with_name('registry-real')
        self.db.parent.rename(moved)
        self.db.parent.symlink_to(moved)
        with self.assertRaisesRegex(ValueError, 'registry_unsafe'):
            registry().routing_rows()
        self.assertEqual(self.register(self.b)['status'], 'unavailable')

    def test_busy_registry_ends_in_bounded_refusal(self):
        self.register(self.a)
        holder = sqlite3.connect(str(self.db), timeout=0)
        self.addCleanup(holder.close)
        holder.execute('BEGIN EXCLUSIVE')
        with self.assertRaisesRegex(ValueError, 'registry_busy'):
            registry().routing_rows(busy_ms=0)
        self.assertEqual(self.register(self.b)['status'], 'unavailable')
        holder.rollback()

    def test_lookup_uses_longest_component_prefix_and_rejects_ties(self):
        # A selected project cannot nest inside another selected one (the
        # selector refuses two markers); the index itself still resolves it.
        self.assertEqual(self.register(self.a)['status'], 'registered')
        nested = self.a.project / 'sub'
        nested.mkdir()
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("INSERT INTO entries(install_id,state_root,project_root,target_root,kind,"
                       "status,observed_at,version) VALUES (?,?,?,?, 'main','active',1,1)",
                       ('8' * 32, str(self.home), str(nested), str(nested)))
            db.commit()
        found = registry().lookup(str(nested / 'x' / 'y.md'))
        self.assertEqual((found['status'], found['row']['installId']), ('owner', '8' * 32))
        self.assertEqual(registry().lookup(str(self.a.project / 'subx' / 'y.md'))['row']['installId'],
                         self.a.install_id)
        self.assertEqual(registry().lookup(str(self.work / 'elsewhere.md'))['status'], 'none')
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("INSERT INTO entries(install_id,state_root,project_root,target_root,kind,"
                       "status,observed_at,version) VALUES (?,?,?,?, 'main','active',1,1)",
                       ('e' * 32, str(self.home), str(self.b.project), str(self.b.project)))
            db.commit()
        self.register(self.b)
        self.assertEqual(registry().lookup(str(self.b.project / 'n.md'))['status'], 'ambiguous')

    def test_concurrent_writers_lose_by_version_not_by_overwrite(self):
        self.register(self.a)
        version = self.row(self.a)['version']
        self.a.project.rename(self.a.project.with_name('alpha-away'))
        seen = []

        def race(target_root, expected):
            # A second connection changes the row between observation and store.
            with contextlib.closing(sqlite3.connect(str(self.db))) as db:
                db.execute("UPDATE entries SET status='conflict', version=version+1 "
                           'WHERE target_root=?', (target_root,))
                db.commit()
            seen.append((target_root, expected))
        with patch.object(registry(), '_before_store', race):
            result = registry().reconcile(now=5000.0, max_rows=8, seconds=60.0, backfill_page=0)
        self.assertEqual(seen, [(str(self.a.project), version)])
        self.assertEqual(result['raced'], 1, result)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['version']), ('conflict', version + 1))

    def test_registration_racing_a_concurrent_change_writes_nothing(self):
        self.register(self.a)
        seen = []

        def race(target_root, expected):
            with contextlib.closing(sqlite3.connect(str(self.db))) as db:
                db.execute("INSERT INTO entries(install_id,state_root,project_root,target_root,"
                           "kind,status,observed_at,version) VALUES (?,?,?,?,'main','active',1,1)",
                           ('e' * 32, str(self.home), target_root, target_root))
                db.commit()
            seen.append((target_root, expected))
        with patch.object(registry(), '_before_store', race):
            result = self.register(self.b)
        self.assertEqual(result['status'], 'raced', result)
        self.assertEqual(seen, [(str(self.b.project), None)])
        self.assertIsNone(self.row(self.b))
        statuses = {iid: row['status'] for (iid, root), row in self.rows().items()
                    if root == str(self.b.project)}
        self.assertEqual(statuses, {'e' * 32: 'active'})

    def test_release_racing_a_concurrent_reregistration_keeps_the_row(self):
        self.register(self.a)
        with patch('laya_fresh_wiki_origin._proof'):
            installs.fresh.restore(self.a.project, self.a.journal)
        version = self.row(self.a)['version']

        def race(target_root, expected):
            with contextlib.closing(sqlite3.connect(str(self.db))) as db:
                db.execute('UPDATE entries SET observed_at=7, version=version+1 '
                           'WHERE target_root=?', (target_root,))
                db.commit()
        with patch.object(registry(), '_before_store', race):
            result = registry().release(str(self.a.project), str(self.a.state), now=2000.0)
        self.assertEqual(result['status'], 'raced', result)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['version']), ('active', version + 1))
        self.assertEqual(registry().release(str(self.a.project), str(self.a.state),
                                            now=3000.0)['status'], 'released')

    def test_registry_db_written_by_this_python_is_readable_by_the_other(self):
        self.register(self.a)
        other = '/usr/bin/python3' if sys.version_info >= (3, 10) else shutil.which('python3.13')
        if not other or not Path(other).exists():
            self.skipTest('second python unavailable')
        code = ('import sys,json; sys.path.insert(0, sys.argv[1]); import memvane_registry as r;'
                'print(json.dumps(sorted(x["installId"] for x in r.routing_rows()["rows"])))')
        out = subprocess.run([other, '-B', '-c', code, str(REPO / 'core')], text=True,
                             capture_output=True, timeout=60,
                             env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [self.a.install_id])


class Lifecycle(Base):
    """Setup commands: register on selected success, detach only on a proved release."""

    def args(self, action, install, *extra):
        return (action, '--project', str(install.project), '--state-root', str(install.state),
                *extra)

    def test_restore_success_detaches_main_and_worktree_rows(self):
        self.register(self.a)
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("INSERT INTO entries(install_id,state_root,project_root,target_root,kind,"
                       "parent_install,status,observed_at,version) "
                       "VALUES (?,?,?,?,'worktree',?,'active',1,1)",
                       (self.a.install_id, str(self.a.state), str(self.a.project),
                        str(self.work / 'alpha-wt'), self.a.install_id))
            db.commit()
        code, result = self.setup(*self.args('restore-index-origin', self.a,
                                             '--approve-index-origin-restore'))
        self.assertEqual((code, result['status']), (0, 'legacy_restored'), result)
        self.assertNotIn('registry', json.dumps(result))
        for target in (self.a.project, self.work / 'alpha-wt'):
            row = self.row(self.a, target)
            self.assertEqual((row['status'], row['reason']), ('detached', 'released'), target)

    def test_blocked_restore_after_first_write_keeps_the_row_active(self):
        self.register(self.a)
        epoch = journal.inspect_journal(self.a.journal)['epoch']
        journal.cas_mark_new_write(self.a.journal, epoch)
        code, result = self.setup(*self.args('restore-index-origin', self.a,
                                             '--approve-index-origin-restore'))
        self.assertEqual((code, result['status']), (1, 'blocked'), result)
        self.assertEqual(self.row(self.a)['status'], 'active')

    def test_recovery_required_and_rejected_keep_the_row_active(self):
        import laya_product_install as product
        self.register(self.a)
        for status in ('recovery_required', 'blocked'):
            with self.subTest(status=status), patch.object(
                    product, 'restore_origin_selection',
                    return_value={'status': status, 'reason': 'x', 'changesApplied': 1}):
                code, result = self.setup(*self.args('restore-index-origin', self.a,
                                                     '--approve-index-origin-restore'))
                self.assertEqual((code, result['status']), (1, status))
                self.assertEqual(self.row(self.a)['status'], 'active')
        code, result = self.setup(*self.args('restore-index-origin', self.a))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.row(self.a)['status'], 'active')

    def test_a_success_status_without_a_released_selection_detaches_nothing(self):
        import laya_product_install as product
        self.register(self.a)
        with patch.object(product, 'restore_origin_selection',
                          return_value={'status': 'legacy_restored', 'changesApplied': 1}):
            code, result = self.setup(*self.args('restore-index-origin', self.a,
                                                 '--approve-index-origin-restore'))
        self.assertEqual((code, result['status']), (0, 'legacy_restored'))
        self.assertEqual(self.row(self.a)['status'], 'active')

    def test_retire_and_abandon_success_detach_existing_rows_and_insert_none(self):
        import laya_product_install as product
        pending = installs.make_install(self.home, self.work / 'gamma', 'gamma',
                                        '8' * 32, '9' * 32, activate=False)
        self.bundles.add(pending)
        # A never-activated build has published no project route.
        (pending.folder / routes.RECORD).unlink()
        self.register(self.a)  # the registry file exists
        cases = (('retire-index-origin', 'retire_drifted_origin',
                  'fresh_wiki_binding_retired', '--approve-index-origin-retire'),
                 ('abandon-index-build', 'abandon_unused_initial_build',
                  'unused_build_retired', '--approve-index-build-abandon'),
                 ('abandon-inactive-index-build', 'abandon_inactive_initial_build',
                  'inactive_build_retired', '--approve-inactive-index-build-abandon'))
        for action, name, status, flag in cases:
            with self.subTest(action=action), patch.object(
                    product, name, return_value={'status': status, 'changesApplied': 1}):
                code, result = self.setup(*self.args(action, pending, flag, '--install-id',
                                                     pending.install_id, '--commit', 'c' * 40))
                self.assertEqual((code, result['status']), (0, status), result)
                self.assertIsNone(self.row(pending))
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("INSERT INTO entries(install_id,state_root,project_root,target_root,kind,"
                       "status,observed_at,version) VALUES (?,?,?,?,'main','active',1,1)",
                       (pending.install_id, str(pending.state), str(pending.project),
                        str(pending.project)))
            db.commit()
        with patch.object(product, 'retire_drifted_origin',
                          return_value={'status': 'fresh_wiki_binding_retired'}):
            self.setup(*self.args('retire-index-origin', pending, '--approve-index-origin-retire'))
        self.assertEqual((self.row(pending)['status'], self.row(pending)['reason']),
                         ('detached', 'released'))

    def test_activation_success_registers_after_reproving_the_route(self):
        import laya_product_install as product
        with patch.object(product, 'activate_origin_selection',
                          return_value={'phase': 'active', 'syncWake': {'status': 'queued'}}):
            code, result = self.setup(*self.args('activate-index-origin', self.a,
                                                 '--approve-index-origin-activation'))
        self.assertEqual(code, 0, result)
        self.assertEqual(result, {'phase': 'active', 'syncWake': {'status': 'queued'}})
        self.assertEqual(self.row(self.a)['status'], 'active')
        pending = installs.make_install(self.home, self.work / 'gamma', 'gamma',
                                        '8' * 32, '9' * 32, activate=False)
        with patch.object(product, 'activate_origin_selection', return_value={'phase': 'active'}):
            self.setup(*self.args('activate-index-origin', pending,
                                  '--approve-index-origin-activation'))
        self.assertIsNone(self.row(pending))

    def test_registry_failure_never_changes_the_setup_result(self):
        import laya_product_install as product
        with patch.object(product, 'activate_origin_selection', return_value={'phase': 'active'}), \
                patch.object(registry(), 'register', side_effect=RuntimeError('boom')):
            code, result = self.setup(*self.args('activate-index-origin', self.a,
                                                 '--approve-index-origin-activation'))
        self.assertEqual((code, result), (0, {'phase': 'active'}))

    def test_explicit_activation_replaces_a_missing_tombstone_backfill_does_not(self):
        self.register(self.a)
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("UPDATE entries SET status='detached', reason='missing', version=version+1")
            db.commit()
        self.backfill()
        self.assertEqual(self.row(self.a)['status'], 'detached')
        self.assertEqual(self.register(self.a, explicit=False)['status'], 'tombstoned')
        self.assertEqual(self.row(self.a)['status'], 'detached')
        self.assertEqual(self.register(self.a, explicit=True)['status'], 'registered')
        self.assertEqual((self.row(self.a)['status'], self.row(self.a)['reason']), ('active', None))


class Reconcile(Base):
    def test_missing_root_is_pending_at_once_and_detached_after_two_observations_and_a_day(self):
        self.register(self.a)
        away = self.a.project.with_name('alpha-away')
        self.a.project.rename(away)
        registry().reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen']), ('missing_pending', 1))
        self.assertEqual(registry().lookup(str(self.a.project / 'x.md'))['status'], 'none')
        registry().reconcile(now=2010.0, backfill_page=0)  # same burst: not independent
        self.assertEqual(self.row(self.a)['missing_seen'], 1)
        registry().reconcile(now=2000.0 + 3600, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'missing_pending')
        registry().reconcile(now=2000.0 + DAY + 1, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['reason']), ('detached', 'missing'))
        away.rename(self.a.project)
        registry().reconcile(now=2000.0 + DAY + 3600, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'detached')

    def test_a_day_alone_or_two_observations_alone_do_not_detach(self):
        self.register(self.a)
        self.a.project.rename(self.a.project.with_name('alpha-away'))
        registry().reconcile(now=2000.0, backfill_page=0)
        registry().reconcile(now=2000.0 + DAY + 5, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'detached')  # 2 obs + a day
        self.register(self.b)
        self.b.project.rename(self.b.project.with_name('beta-away'))
        for step in range(5):
            registry().reconcile(now=10000.0 + step * 120, backfill_page=0)
        self.assertEqual(self.row(self.b)['status'], 'missing_pending')

    def test_an_emptied_registry_is_never_reinitialized_or_backfilled(self):
        self.register(self.a)
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("UPDATE entries SET status='detached', reason='missing', version=version+1")
            db.commit()
        self.db.write_bytes(b'')
        result = registry().reconcile(now=2000.0, max_rows=64, seconds=60.0, backfill_page=64)
        self.assertEqual((result['status'], result.get('reason')),
                         ('unavailable', 'registry_corrupt'), result)
        self.assertEqual(self.db.read_bytes(), b'')
        self.assertEqual(self.register(self.a)['status'], 'unavailable')
        self.assertEqual(self.db.read_bytes(), b'')
        with self.assertRaisesRegex(ValueError, 'registry_corrupt'):
            registry().routing_rows()

    def test_implicit_registration_never_adopts_a_replaced_root(self):
        self.register(self.a)
        inode = self.row(self.a)['inode']
        moved = self.a.project.with_name('alpha-old')
        shutil.copytree(self.a.project, moved, symlinks=True)
        shutil.rmtree(self.a.project)
        moved.rename(self.a.project)  # same path and route, new inode
        self.assertNotEqual(self.a.project.stat().st_ino, inode)
        # Backfill reaches the install before any row check does.
        registry().reconcile(now=2000.0, max_rows=0, seconds=60.0, backfill_page=64)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['reason'], row['inode']),
                         ('detached', 'root_moved', inode))
        self.assertEqual(self.register(self.a, explicit=False, now=3000.0)['status'],
                         'tombstoned')
        self.assertEqual(self.row(self.a)['status'], 'detached')

    def missing_twice_then_back(self):
        self.register(self.a)
        away = self.a.project.with_name('alpha-away')
        self.a.project.rename(away)
        registry().reconcile(now=2000.0, backfill_page=0)
        registry().reconcile(now=2000.0 + 3600, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen']), ('missing_pending', 2))
        away.rename(self.a.project)  # the original inode returns after the grace

    def test_a_root_back_after_the_grace_is_not_reactivated_by_reconcile(self):
        self.missing_twice_then_back()
        registry().reconcile(now=2000.0 + DAY + 10, backfill_page=0)
        row = self.row(self.a)
        self.assertNotEqual(row['status'], 'active')
        self.assertEqual(row['missing_since'], 2000.0)

    def test_a_root_back_after_the_grace_is_not_reactivated_by_implicit_registration(self):
        self.missing_twice_then_back()
        registry().reconcile(now=2000.0 + DAY + 10, max_rows=0, seconds=60.0,
                             backfill_page=64)
        self.register(self.a, explicit=False, now=2000.0 + DAY + 20)
        row = self.row(self.a)
        self.assertNotEqual(row['status'], 'active')
        self.assertEqual(row['missing_since'], 2000.0)
        self.assertEqual(self.register(self.a, explicit=True, now=2000.0 + DAY + 30)['status'],
                         'registered')
        self.assertEqual(self.row(self.a)['status'], 'active')

    def test_one_sighting_and_a_day_neither_reactivates_nor_resets(self):
        self.register(self.a)
        away = self.a.project.with_name('alpha-away')
        self.a.project.rename(away)
        registry().reconcile(now=2000.0, backfill_page=0)
        away.rename(self.a.project)
        registry().reconcile(now=2000.0 + DAY + 10, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_since'], row['missing_seen']),
                         ('missing_pending', 2000.0, 1))

    def test_return_within_grace_restores_active_inode_change_detaches(self):
        self.register(self.a)
        self.register(self.b)
        away = self.a.project.with_name('alpha-away')
        self.a.project.rename(away)
        registry().reconcile(now=2000.0, backfill_page=0)
        away.rename(self.a.project)
        registry().reconcile(now=2000.0 + 600, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen'], row['missing_since']),
                         ('active', 0, None))
        moved = self.b.project.with_name('beta-old')
        shutil.copytree(self.b.project, moved, symlinks=True)
        shutil.rmtree(self.b.project)
        moved.rename(self.b.project)  # same path, new inode
        registry().reconcile(now=3000.0, backfill_page=0)
        row = self.row(self.b)
        self.assertEqual((row['status'], row['reason']), ('detached', 'root_moved'))

    def test_unobservable_root_is_not_counted_as_missing(self):
        self.register(self.a)
        real_stat = os.stat
        target = str(self.a.project)

        def denied(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError(13, 'denied', target)
            return real_stat(path, *args, **kwargs)
        with patch.object(os, 'stat', denied):
            registry().reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_seen']), ('active', 0))

    def test_future_missing_time_neither_sticks_nor_detaches_at_once(self):
        self.register(self.a)
        self.a.project.rename(self.a.project.with_name('alpha-away'))
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("UPDATE entries SET status='missing_pending', missing_since=?, "
                       "missing_seen=5, missing_last=?, version=version+1", (1e12, 1e12))
            db.commit()
        registry().reconcile(now=5000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['missing_since'], row['missing_seen']),
                         ('missing_pending', 5000.0, 1))
        registry().reconcile(now=5000.0 + DAY + 1, backfill_page=0)
        self.assertEqual(self.row(self.a)['status'], 'detached')

    def test_reconcile_never_recovers_another_install_journal(self):
        self.register(self.b)
        # A crashed writer leaves the v4 journal torn with a hot rollback file.
        script = ('import os, sqlite3, sys; '
                  'db = sqlite3.connect(sys.argv[1], isolation_level=None); '
                  "db.execute('PRAGMA cache_size=1'); db.execute('BEGIN IMMEDIATE'); "
                  "db.execute('CREATE TABLE junk(x)'); "
                  "[db.execute('INSERT INTO junk VALUES(randomblob(4000))') for _ in range(300)]; "
                  'os._exit(9)')
        done = subprocess.run([sys.executable, '-B', '-c', script, str(self.b.journal)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 9, done.stderr)
        side = Path(str(self.b.journal) + '-journal')
        self.assertTrue(side.exists())
        before = (sha(self.b.journal), sha(side))
        self.backfill(now=2000.0)
        registry().reconcile(now=3000.0, backfill_page=0)
        self.assertTrue(side.exists(), 'the registry rolled back another install journal')
        self.assertEqual((sha(self.b.journal), sha(side)), before)

    def test_old_bundle_release_is_observed_as_released(self):
        self.register(self.a)
        with patch('laya_fresh_wiki_origin._proof'):
            installs.fresh.restore(self.a.project, self.a.journal)
        self.assertEqual(routes.select_route(str(self.a.project), action='index')['state'], 'legacy')
        registry().reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual((row['status'], row['reason']), ('detached', 'released'))

    def test_unproved_route_marks_proof_but_never_detaches(self):
        self.register(self.a)
        installs.private_write(self.a.folder / routes.RECORD, b'{"schema":')
        self.assertEqual(routes.select_route(str(self.a.project), action='index')['state'], 'blocked')
        registry().reconcile(now=2000.0, backfill_page=0)
        row = self.row(self.a)
        self.assertEqual(row['status'], 'active')
        self.assertTrue(row['proof'].startswith('unproved'), row['proof'])

    def test_row_budget_and_cursor_cover_every_row_across_runs(self):
        extra = [installs.make_install(self.home, self.work / f'p{i}', f'p{i}',
                                       f'{i:032x}', f'{i + 100:032x}') for i in range(1, 6)]
        for install in (self.a, self.b, *extra):
            self.bundles.add(install)
            self.register(install)
        first = registry().reconcile(now=2000.0, max_rows=3, seconds=60.0, backfill_page=0)
        second = registry().reconcile(now=2001.0, max_rows=3, seconds=60.0, backfill_page=0)
        third = registry().reconcile(now=2002.0, max_rows=3, seconds=60.0, backfill_page=0)
        self.assertEqual((first['checked'], second['checked'], third['checked']), (3, 3, 3))
        observed = {row['install_id'] for row in self.rows().values() if row['observed_at'] >= 2000.0}
        self.assertEqual(len(observed), 7)

    def test_time_budget_stops_by_injected_clock(self):
        for install in (self.a, self.b):
            self.register(install)
        ticks = iter(range(0, 1000))
        result = registry().reconcile(now=2000.0, max_rows=32, seconds=1.5,
                                      clock=lambda: float(next(ticks)), backfill_page=0)
        self.assertEqual(result['checked'], 1, result)

    def test_one_failing_row_does_not_block_the_next(self):
        for install in (self.a, self.b):
            self.register(install)
        def flaky(real):
            def classify(cwd, action='recall'):
                if cwd == str(self.a.project):
                    raise RuntimeError('boom')
                return real(cwd, action=action)
            return classify
        self.b.project.rename(self.b.project.with_name('beta-away'))
        with patch.object(routes, 'select_route', flaky(routes.select_route)), \
                patch.object(routes, '_select_route', flaky(routes._select_route)):
            result = registry().reconcile(now=2000.0, backfill_page=0)
        self.assertEqual(result['checked'], 2, result)
        self.assertEqual(self.row(self.b)['status'], 'missing_pending')





    def test_backfill_never_resurrects_a_tombstone_or_overwrites_another_install(self):
        self.register(self.a)
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("UPDATE entries SET status='detached', reason='missing'")
            db.execute("INSERT INTO entries(install_id,state_root,project_root,target_root,kind,"
                       "status,observed_at,version) VALUES (?,?,?,?,'main','active',1,1)",
                       ('e' * 32, str(self.home / 'gone'), str(self.b.project),
                        str(self.b.project)))
            db.commit()
        self.backfill()
        self.assertEqual(self.row(self.a)['status'], 'detached')
        statuses = {row['install_id']: row['status'] for row in self.rows().values()
                    if row['target_root'] == str(self.b.project)}
        self.assertEqual(statuses, {'e' * 32: 'conflict', self.b.install_id: 'conflict'})
        self.assertEqual(registry().lookup(str(self.b.project / 'n.md'))['status'], 'ambiguous')
        self.assertNotIn(self.b.install_id,
                         [row['installId'] for row in registry().routing_rows()['rows']])

    @unittest.skipUnless(shutil.which('git'), 'git unavailable')
    def test_linked_worktrees_register_with_main_and_detach_after_prune(self):
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
               '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main']
        run = lambda *args: subprocess.run([*git, *args], cwd=self.b.project, check=True,
                                           capture_output=True, text=True, timeout=30)
        (self.b.project / 'note.md').write_text('B\n')
        run('init', '-q')
        run('add', 'note.md', '.memvane/settings.json')
        run('commit', '-qm', 'init')
        worktree = (self.work / 'beta-wt')
        run('worktree', 'add', '-q', str(worktree))
        self.assertEqual(self.register(self.b)['status'], 'registered')
        row = self.row(self.b, worktree)
        self.assertEqual((row['kind'], row['status'], row['parent_install']),
                         ('worktree', 'active', self.b.install_id))
        found = registry().lookup(str(worktree / 'note.md'))
        self.assertEqual(found['row']['kind'], 'worktree')
        self.assertNotIn(str(worktree), [r['targetRoot'] for r in registry().routing_rows()['rows']])
        with patch('git_branch_safety.run_git', side_effect=__import__(
                'git_branch_safety').GitUnavailable('git_call_failed')):
            registry().reconcile(now=2000.0, backfill_page=0)
        self.assertEqual(self.row(self.b, worktree)['status'], 'active')
        calls = []
        real = routes.select_route
        with patch.object(routes, 'select_route',
                          side_effect=lambda cwd, action='recall': calls.append(cwd) or
                          real(cwd, action=action)):
            deadline = __import__('time').monotonic() + 60
            self.assertIsNone(registry()._linked(self.b.project, deadline,
                                                 clock=lambda: deadline + 1))
            self.assertIsNone(registry()._linked(self.b.project,
                                                 __import__('time').monotonic() - 1))
            registry().reconcile(now=2500.0, backfill_page=0, seconds=0.0)
        self.assertEqual(calls, [])
        self.assertEqual(self.row(self.b, worktree)['status'], 'active')
        run('worktree', 'remove', '--force', str(worktree))
        registry().reconcile(now=3000.0, backfill_page=0)
        row = self.row(self.b, worktree)
        self.assertEqual((row['status'], row['reason']), ('detached', 'released'))
        self.assertEqual(self.row(self.b)['status'], 'active')


class SessionStart(Base):
    """The selected SessionStart hook reconciles opportunistically and stays bounded."""

    def setUp(self):
        super().setUp()
        proofs = {str(i.project): {'status': 'installer_active_full_plugin_verified',
                                   'productRouteAllowed': True, 'selectionEpoch': 2,
                                   'executingPluginRoot': LOADED_ROOT,
                                   'projectRoot': str(i.project), 'installId': i.install_id,
                                   'providerManifest': {'providerPath': 'p.py'}}
                  for i in (self.a, self.b)}
        self.patch(journal, 'verify_selected_route',
                   side_effect=lambda decision, action='update': dict(proofs[decision['projectRoot']]))
        self.patch(runtime_work, 'request', return_value={'status': 'queued'})
        self.patch(response, '_request_wiki_lifecycle')

    def session_start(self, install):
        event = {'cwd': str(install.project), 'hook_event_name': 'SessionStart',
                 'session_id': 'test-session', 'source': 'startup'}
        decision = routes.select_route(str(install.project), action='update-queued')
        return response.serve_selected('update-queued', 'claude', decision, event)

    def test_session_start_registers_the_selected_project_and_backfills(self):
        self.session_start(self.a)
        self.assertEqual(self.row(self.a)['status'], 'active')
        for _ in range(4):
            self.session_start(self.a)
        self.assertEqual(self.row(self.b)['status'], 'active')

    def test_session_start_budget_is_bounded_by_rows(self):
        calls = []
        real = registry().reconcile

        def spy(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)
        with patch.object(registry(), 'reconcile', side_effect=spy):
            self.session_start(self.a)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0]['max_rows'], registry().HOOK_ROWS)
        self.assertLessEqual(calls[0]['backfill_page'], registry().HOOK_BACKFILL_PAGE)
        self.assertLessEqual(calls[0]['seconds'], registry().HOOK_SECONDS)
        self.assertLessEqual(calls[0]['busy_ms'], registry().HOOK_BUSY_MS)

    def test_session_start_response_is_unchanged_by_registry_state(self):
        # REG: no registry API is used, so the base release answers the same.
        baseline = self.session_start(self.a)
        self.assertIn('selected source update queued',
                      baseline['hookSpecificOutput']['additionalContext'])
        directory = memvane_paths.state_base() / 'auto-context' / 'registry'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / 'projects.sqlite'
        for case in ('corrupt', 'busy', 'unsafe'):
            with self.subTest(case=case):
                for entry in (path, directory):
                    if entry.is_symlink() or entry.is_file():
                        entry.unlink()
                directory.mkdir(mode=0o700, exist_ok=True)
                holder = None
                if case == 'corrupt':
                    path.write_bytes(b'not a database' * 64)
                    path.chmod(0o600)
                elif case == 'busy':
                    holder = sqlite3.connect(str(path), timeout=0)
                    holder.execute('CREATE TABLE x(y)')
                    holder.commit()
                    path.chmod(0o600)
                    holder.execute('BEGIN EXCLUSIVE')
                else:
                    shutil.rmtree(directory)
                    real = directory.with_name('registry-real')
                    real.mkdir(mode=0o700, exist_ok=True)
                    directory.symlink_to(real)
                try:
                    self.assertEqual(self.session_start(self.a), baseline)
                finally:
                    if holder is not None:
                        holder.rollback()
                        holder.close()
                if case == 'corrupt':
                    self.assertEqual(path.read_bytes(), b'not a database' * 64)
                if case == 'unsafe':
                    directory.unlink()

    def test_session_start_reconciles_only_inside_the_hook_budget(self):
        import hook_budget
        baseline = self.session_start(self.a)
        calls = []
        with patch.object(registry(), 'reconcile',
                          side_effect=lambda **kwargs: calls.append(kwargs) or {}):
            with patch.object(hook_budget, 'remaining', return_value=0.6):
                self.assertEqual(self.session_start(self.a), baseline)
            self.assertEqual(calls, [])
            with patch.object(hook_budget, 'remaining', return_value=1.2):
                self.assertEqual(self.session_start(self.a), baseline)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0]['seconds'], 0.2 + 1e-9)

    @unittest.skipUnless(shutil.which('git'), 'git unavailable')
    def test_session_start_reconcile_runs_no_git(self):
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
               '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main']
        run = lambda *args: subprocess.run([*git, *args], cwd=self.b.project, check=True,
                                           capture_output=True, text=True, timeout=30)
        (self.b.project / 'note.md').write_text('B\n')
        run('init', '-q')
        run('add', 'note.md', '.memvane/settings.json')
        run('commit', '-qm', 'init')
        worktree = self.work / 'beta-wt'
        run('worktree', 'add', '-q', str(worktree))
        self.assertEqual(routes.select_route(str(worktree), action='index')['state'], 'worktree')
        calls, results = [], []
        real_run = subprocess.run
        real_reconcile = registry().reconcile

        def spy_run(args, *rest, **kwargs):
            if isinstance(args, (list, tuple)) and args and str(args[0]).endswith('git'):
                calls.append(list(args))
            return real_run(args, *rest, **kwargs)

        def spied(**kwargs):
            with patch.object(subprocess, 'run', spy_run):
                results.append(real_reconcile(**kwargs))
                return results[-1]
        with patch.object(registry(), 'reconcile', side_effect=spied):
            self.session_start(self.b)
            # A second pass reconciles the main row (seeded by the first).
            self.session_start(self.b)
        self.assertEqual(calls, [], 'the SessionStart reconcile ran git')
        self.assertEqual([r['status'] for r in results], ['ok', 'ok'])
        self.assertEqual(self.row(self.b)['status'], 'active')
        self.assertIsNone(self.row(self.b, worktree))

    def test_session_start_with_an_exhausted_budget_keeps_the_response(self):
        import hook_budget
        baseline = self.session_start(self.a)
        with patch.object(hook_budget, 'remaining',
                          side_effect=hook_budget.HookDeadlineExceeded()):
            self.assertEqual(self.session_start(self.a), baseline)

    def test_hook_reconcile_journal_waits_are_bounded_by_its_budget(self):
        timeouts = []
        real_connect = sqlite3.connect
        real_reconcile = registry().reconcile

        def connect(target, *args, **kwargs):
            if str(target).split('?')[0].endswith('install-journal.sqlite'):
                timeouts.append(kwargs.get('timeout', 5.0))
            return real_connect(target, *args, **kwargs)

        def spied(**kwargs):
            with patch.object(sqlite3, 'connect', connect):
                return real_reconcile(**kwargs)
        with patch.object(registry(), 'reconcile', side_effect=spied):
            self.session_start(self.a)
        self.assertNotEqual(timeouts, [])
        self.assertLessEqual(max(timeouts), registry().HOOK_SECONDS)

    def test_session_start_registry_failure_keeps_the_hook_output(self):
        baseline = self.session_start(self.a)
        with patch.object(registry(), 'reconcile', side_effect=RuntimeError('boom')):
            self.assertEqual(self.session_start(self.a), baseline)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *sys.argv[1:]], verbosity=2)
