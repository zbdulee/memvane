"""External edit routing over the selected project registry, and mixed bundles.

A complete, healthy registry answers ``laya_external_edits`` target discovery
without listing a state base. An absent, corrupt, busy or unsupported registry
refuses (no fallback scan). While the backfill is incomplete the existing scan
runs with the registry's tombstones applied. A registry row is a candidate
only: the same journal and route proof decides.

The mixed-bundle checks run the base release's ``core/`` (``git archive``)
next to a registry this bundle wrote and require the same results as without
one.
"""
from __future__ import annotations

import contextlib
import hashlib
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
import laya_external_edits as external  # noqa: E402
import laya_index_host_route as routes  # noqa: E402
import laya_install_journal as journal  # noqa: E402
import memvane_paths  # noqa: E402
import runtime_work  # noqa: E402
import out_of_project_installs as installs  # noqa: E402

try:
    import memvane_registry
except ImportError:
    memvane_registry = None

BASE_COMMIT = os.environ.get('MEMVANE_REGISTRY_BASE_COMMIT', '4510105e')


def registry():
    if memvane_registry is None:
        raise AssertionError('core/memvane_registry.py is missing')
    return memvane_registry


class Routing(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='registry-route-')
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
        self.wake = self.patch(runtime_work, 'request', return_value={'status': 'queued'})
        self.file = self.b.project / 'note.md'
        self.file.write_text('B knowledge\n')
        self.scans = []
        real = os.scandir

        def counted(path='.'):
            if (isinstance(path, (str, os.PathLike)) and
                    Path(path) in (memvane_paths.state_base(), memvane_paths.legacy_state_base())):
                self.scans.append(str(path))
            return real(path)
        self.patch(os, 'scandir', side_effect=counted)

    def patch(self, obj, name, **kw):
        patcher = patch.object(obj, name, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    @property
    def db(self):
        return memvane_paths.state_base() / 'registry' / 'projects.sqlite'

    def backfill(self):
        for _ in range(8):
            if registry().reconcile(now=1000.0, max_rows=64, seconds=60.0,
                                    backfill_page=64)['complete']:
                return
        self.fail('backfill incomplete')

    def notify(self, path=None):
        self.scans.clear()
        return external.notify([str(path or self.file)], cwd=str(self.a.project),
                               host='codex', origin=str(self.a.project))

    def routed(self, install=None):
        return ('laya-sync', str((install or self.b).project)) in [
            call.args[:2] for call in self.wake.call_args_list]


class RegistryTargets(Routing):
    def test_absent_registry_refuses_without_a_state_base_scan(self):
        result = self.notify()
        self.assertEqual(result, {'owners': 0, 'failed': 1, 'overflow': 0})
        self.assertFalse(self.routed())
        self.assertEqual(self.scans, [])
        self.assertFalse(self.db.exists())

    def test_complete_registry_routes_without_listing_any_state_base(self):
        self.backfill()
        result = self.notify()
        self.assertEqual(result['owners'], 1, result)
        self.assertTrue(self.routed())
        self.assertEqual(self.scans, [])

    def test_corrupt_unsupported_and_busy_registries_refuse_and_are_preserved(self):
        self.backfill()
        good = self.db.read_bytes()
        cases = {'corrupt': lambda: self.db.write_bytes(b'x' * 4096)}

        def unsupported():
            with contextlib.closing(sqlite3.connect(str(self.db))) as db:
                db.execute("UPDATE meta SET value='memvane-project-registry-v9' WHERE key='schema'")
                db.commit()
        cases['unsupported'] = unsupported
        for name, damage in cases.items():
            with self.subTest(case=name):
                self.db.write_bytes(good)
                damage()
                before = hashlib.sha256(self.db.read_bytes()).hexdigest()
                self.wake.reset_mock()
                self.assertEqual(self.notify()['failed'], 1)
                self.assertFalse(self.routed())
                self.assertEqual(self.scans, [])
                self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.db.write_bytes(good)
        holder = sqlite3.connect(str(self.db), timeout=0)
        self.addCleanup(holder.close)
        holder.execute('BEGIN EXCLUSIVE')
        self.wake.reset_mock()
        self.assertEqual(self.notify()['failed'], 1)
        self.assertFalse(self.routed())
        holder.rollback()

    def test_registry_row_without_a_current_route_proof_does_not_route(self):
        self.backfill()
        installs.private_write(self.b.folder / routes.RECORD, b'{"schema":')
        self.assertEqual(self.notify()['owners'], 0)
        self.assertFalse(self.routed())

    def test_missing_pending_detached_and_conflict_rows_do_not_route(self):
        self.backfill()
        for status, reason in (('missing_pending', None), ('detached', 'missing'),
                               ('conflict', None)):
            with self.subTest(status=status):
                with contextlib.closing(sqlite3.connect(str(self.db))) as db:
                    db.execute('UPDATE entries SET status=?, reason=?, version=version+1 '
                               'WHERE install_id=?', (status, reason, self.b.install_id))
                    db.commit()
                self.wake.reset_mock()
                self.assertEqual(self.notify()['owners'], 0)
                self.assertFalse(self.routed())

    def test_unregistered_nested_project_is_not_routed_to_its_parent(self):
        self.backfill()
        nested = self.b.project / 'nested'
        (nested / '.memvane').mkdir(parents=True)
        (nested / '.memvane' / 'settings.json').write_text('{"name": "nested", "skipPaths": []}\n')
        path = nested / 'note.md'
        path.write_text('nested\n')
        self.assertEqual(self.notify(path)['owners'], 0)
        self.assertFalse(self.routed())

    def test_registry_rows_over_the_install_cap_refuse(self):
        self.backfill()
        with patch.object(external, 'MAX_INSTALLS', 1):
            self.assertEqual(self.notify()['failed'], 1)
        self.assertFalse(self.routed())

    def test_incomplete_backfill_scans_with_tombstones_applied(self):
        self.backfill()
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("UPDATE scans SET complete_at=NULL")
            db.commit()
        self.assertEqual(self.notify()['owners'], 1)
        self.assertNotEqual(self.scans, [])
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            db.execute("UPDATE entries SET status='detached', reason='missing' "
                       'WHERE install_id=?', (self.b.install_id,))
            db.commit()
        self.wake.reset_mock()
        self.assertEqual(self.notify()['owners'], 0)
        self.assertFalse(self.routed())


class MixedBundle(Routing):
    """The base release's core reads the same targets with or without a registry."""

    def setUp(self):
        super().setUp()
        temp = tempfile.mkdtemp(prefix='registry-oldcore-')
        self.addCleanup(shutil.rmtree, temp, True)
        self.old = Path(temp).resolve()
        archive = subprocess.run(['git', 'archive', BASE_COMMIT, 'core', 'test/fixtures'],
                                 cwd=REPO, capture_output=True, timeout=120)
        # The mixed-bundle regression needs the base release; never pass vacuously.
        self.assertEqual(archive.returncode, 0,
                         'base commit unavailable: ' + archive.stderr.decode()[:200])
        subprocess.run(['tar', '-x', '-C', str(self.old)], input=archive.stdout, check=True,
                       timeout=120)

    def old_targets(self):
        code = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1] + '/core'); sys.path.insert(1, sys.argv[1] + '/test/fixtures')
from unittest import mock
import laya_install_journal as journal, laya_external_edits as ext
proofs = json.loads(sys.argv[3])
with mock.patch.object(journal, 'inspect_full_plugin_candidate', side_effect=lambda r: dict(proofs[str(r)])):
    targets = ext._targets(sys.argv[2], time.monotonic() + 30)
print(json.dumps(sorted((t['projectRoot'], t['rawRoot']) for t in targets)))
'''
        proofs = {i.proof['pluginRoot']: i.proof for i in (self.a, self.b)}
        out = subprocess.run([sys.executable, '-B', '-c', code, str(self.old),
                              str(self.a.project), json.dumps(proofs)],
                             capture_output=True, text=True, timeout=120,
                             env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_old_core_targets_are_unchanged_by_a_registry_file(self):
        without = self.old_targets()
        self.assertEqual(without, [[str(self.b.project), str(self.b.project)]])
        self.backfill()
        self.assertTrue(self.db.exists())
        self.assertEqual(self.old_targets(), without)

    def test_old_core_activation_is_found_by_a_later_backfill(self):
        self.backfill()
        late = installs.make_install(self.home, self.work / 'gamma', 'gamma', '8' * 32, '9' * 32)
        self.bundles.add(late)
        registry().reconcile(now=99999.0, max_rows=64, seconds=60.0, backfill_page=64)
        found = registry().routing_rows()['rows']
        self.assertIn(late.install_id, [row['installId'] for row in found])


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *sys.argv[1:]], verbosity=2)
