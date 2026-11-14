"""Owner diagnostic-target registry (Task 4): RED contract for ``log_targets``.

Contract pinned here (module not yet implemented):

* ``log_targets.register(projects: list[str]) -> dict`` returns
  ``{'results': [row, ...], 'partial': bool}``; one row per requested project,
  in request order: ``{'requested', 'root', 'install_id', 'status'}`` where
  ``status`` is ``'registered'``, ``'duplicate'`` or a fixed refusal.
* ``log_targets.resolve() -> list[dict]``: one row per registered target,
  ``{'root', 'install_id', 'state_root'}`` when the current route still binds
  the same install, else ``{'root', 'install_id', 'refusal'}``.
* ``log_targets.route_for(root: Path) -> dict | None`` is the only route
  lookup; tests patch it. It returns ``{'install_id', 'state_root',
  'onboarded'}`` or ``None`` (missing install).
* ``log_targets.registry_path()`` is
  ``$XDG_CONFIG_HOME/memvane/log-targets.json``; ``MAX_TARGETS == 64``.
* Refusals: ``missing``, ``not_onboarded``, ``stale_route``,
  ``limit_exceeded``, ``unsafe_registry``, ``registry_corrupt``,
  ``registry_busy``.

Only synthetic temporary directories under the wrapper's temporary HOME are
used; no real install, route or user file is touched and no child process is
spawned.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tests.support.case import require_fixture_env

SENTINEL = 'SYNTH-RAW-SENTINEL-do-not-store'
REFUSALS = {'missing', 'not_onboarded', 'stale_route', 'limit_exceeded',
            'unsafe_registry', 'registry_corrupt', 'registry_busy'}


def lt():
    from memvane.registry import log_targets
    return log_targets


class Base(unittest.TestCase):
    def setUp(self):
        home = require_fixture_env()
        self.home = home
        self.work = Path(tempfile.mkdtemp(dir=home, prefix='targets-'))
        self.cfg = self.work / '.config'
        env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)
        self.routes = {}
        self.mod = lt()
        p = patch.object(self.mod, 'route_for', side_effect=self._route)
        p.start()
        self.real_route_for = p.temp_original
        self.addCleanup(p.stop)
        reg = self.mod.registry_path()
        if reg.exists() or reg.is_symlink():
            reg.unlink()

    def _route(self, root):
        return self.routes.get(Path(root).resolve())

    def install(self, name, *, onboarded=True, install_id=None):
        root = self.work / name
        state = root / '.memvane-state'
        state.mkdir(parents=True, exist_ok=True)
        self.routes[root.resolve()] = {
            'install_id': install_id or f'install-{name}',
            'state_root': state, 'onboarded': onboarded}
        return root

    def registry_lines(self):
        return self.mod.registry_path().read_bytes().splitlines()


class Contract(Base):
    def test_real_route_lookup_checks_project_and_selection(self):
        root = self.install('route')
        decision = {'state': 'candidate', 'projectRoot': str(root),
                    'installId': 'install-route',
                    'journalPath': str(root / '.memvane-state' / 'install-journal.sqlite')}
        with patch('memvane.journal.api.select_route', return_value=decision):
            self.assertEqual(self.real_route_for(root)['install_id'], 'install-route')
        with patch('memvane.journal.api.select_route', return_value={**decision, 'projectRoot': str(self.work)}):
            self.assertIsNone(self.real_route_for(root))
        with patch('memvane.journal.api.select_route', return_value={'state': 'legacy'}):
            self.assertFalse(self.real_route_for(root)['onboarded'])

    def test_missing_registry_is_reported(self):
        self.assertEqual(self.mod.resolve()[0]['refusal'], 'registry_missing')

    def test_path_and_limit(self):
        self.assertEqual(self.mod.registry_path(),
                         self.cfg / 'memvane' / 'log-targets.json')
        self.assertEqual(self.mod.MAX_TARGETS, 64)

    def test_nine_distinct_installs_register_with_expected_ids(self):
        roots = [self.install(f'p{i}') for i in range(9)]
        out = self.mod.register([str(r) for r in roots])
        self.assertFalse(out['partial'])
        self.assertEqual([r['status'] for r in out['results']], ['registered'] * 9)
        self.assertEqual([r['install_id'] for r in out['results']],
                         [f'install-p{i}' for i in range(9)])
        rows = self.mod.resolve()
        self.assertEqual(len(rows), 9)
        for i, row in enumerate(sorted(rows, key=lambda r: r['install_id'])):
            self.assertEqual(row['install_id'], f'install-p{i}')
            self.assertEqual(Path(row['state_root']), roots[i] / '.memvane-state')
            self.assertNotIn('refusal', row)

    def test_alias_dedup_by_canonical_root(self):
        root = self.install('canon')
        alias = self.work / 'alias-link'
        alias.symlink_to(root)
        out = self.mod.register([str(root), str(alias), str(root) + '/'])
        self.assertEqual([r['status'] for r in out['results']],
                         ['registered', 'duplicate', 'duplicate'])
        self.assertEqual({r['root'] for r in out['results']}, {str(root.resolve())})
        self.assertEqual(len(self.mod.resolve()), 1)

    def test_bad_one_does_not_suppress_others(self):
        good = [self.install('g1'), self.install('g2')]
        fresh = self.install('fresh', onboarded=False)
        missing = self.work / 'nowhere'
        out = self.mod.register([str(good[0]), str(fresh), str(missing), str(good[1])])
        self.assertTrue(out['partial'])
        self.assertEqual([r['status'] for r in out['results']],
                         ['registered', 'not_onboarded', 'missing', 'registered'])
        self.assertEqual(len(self.mod.resolve()), 2)

    def test_same_install_id_is_deduplicated_across_roots(self):
        first = self.install('shared-a', install_id='install-shared')
        second = self.install('shared-b', install_id='install-shared')
        result = self.mod.register([str(first), str(second)])
        self.assertEqual([row['status'] for row in result['results']],
                         ['registered', 'duplicate'])
        self.assertEqual(len(self.mod.resolve()), 1)

    def test_resolve_detects_route_change_without_hiding_others(self):
        a, b = self.install('a'), self.install('b')
        self.mod.register([str(a), str(b)])
        self.routes[a.resolve()]['install_id'] = 'install-replaced'
        rows = {Path(r['root']).name: r for r in self.mod.resolve()}
        self.assertEqual(rows['a']['refusal'], 'stale_route')
        self.assertEqual(rows['a']['install_id'], 'install-a')
        self.assertNotIn('state_root', rows['a'])
        self.assertEqual(Path(rows['b']['state_root']), b / '.memvane-state')
        del self.routes[b.resolve()]
        rows = {Path(r['root']).name: r for r in self.mod.resolve()}
        self.assertEqual(rows['b']['refusal'], 'missing')

    def test_target_limit_64(self):
        roots = [self.install(f'm{i:02d}') for i in range(65)]
        out = self.mod.register([str(r) for r in roots])
        self.assertTrue(out['partial'])
        statuses = [r['status'] for r in out['results']]
        self.assertEqual(statuses[:64], ['registered'] * 64)
        self.assertEqual(statuses[64], 'limit_exceeded')
        self.assertEqual(len(self.mod.resolve()), 64)

    def test_registry_holds_only_root_and_install_id(self):
        root = self.install('quiet')
        self.mod.register([str(root)])
        data = self.mod.registry_path().read_bytes()
        self.assertNotIn(SENTINEL.encode(), data)
        for line in self.registry_lines():
            row = json.loads(line.decode('utf-8'))
            self.assertLessEqual(set(row), {'version', 'op', 'root', 'install_id'})
            self.assertIn(row['op'], {'upsert', 'remove'})
            for banned in ('message', 'raw', 'body', 'state_root', 'trace'):
                self.assertNotIn(banned, row)


class FileSafety(Base):
    def seed(self):
        root = self.install('seed')
        self.mod.register([str(root)])
        return root

    def test_owner_private_registry(self):
        self.seed()
        info = os.lstat(self.mod.registry_path())
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_nlink, 1)
        parent = os.lstat(self.mod.registry_path().parent)
        self.assertFalse(stat.S_IMODE(parent.st_mode) & 0o077)

    def assert_refused(self, root):
        out = self.mod.register([str(root)])
        self.assertTrue(out['partial'])
        self.assertEqual(out['results'][0]['status'], 'unsafe_registry')
        self.assertEqual([r.get('refusal') for r in self.mod.resolve()], ['unsafe_registry'])

    def test_symlink_registry_refused(self):
        self.mod.registry_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        decoy = self.work / 'decoy.jsonl'
        decoy.write_bytes(b'')
        os.chmod(decoy, 0o600)
        self.mod.registry_path().symlink_to(decoy)
        self.assert_refused(self.install('s'))
        self.assertEqual(decoy.read_bytes(), b'')

    def test_hardlink_registry_refused(self):
        self.seed()
        os.link(self.mod.registry_path(), self.work / 'second-name')
        self.assert_refused(self.install('h'))

    def test_loose_mode_registry_refused(self):
        self.seed()
        os.chmod(self.mod.registry_path(), 0o644)
        self.assert_refused(self.install('m'))

    def test_symlinked_config_dir_refused(self):
        real = self.work / 'real-cfg'
        real.mkdir(mode=0o700)
        self.mod.registry_path().parent.parent.mkdir(parents=True, exist_ok=True)
        link = self.mod.registry_path().parent
        if link.exists():
            for child in link.iterdir():
                child.unlink()
            link.rmdir()
        link.symlink_to(real)
        self.assert_refused(self.install('d'))
        self.assertEqual(list(real.iterdir()), [])


class JournalRecovery(Base):
    def test_partial_final_line_trimmed_prior_rows_kept(self):
        a = self.install('a')
        self.mod.register([str(a)])
        reg = self.mod.registry_path()
        ino = os.lstat(reg).st_ino
        with open(reg, 'ab') as fh:
            fh.write(b'{"version":1,"op":"upsert","root":"/tr')
        before_recovery = self.mod.resolve()
        self.assertTrue(any(r.get('refusal') == 'registry_incomplete' for r in before_recovery))
        self.assertTrue(any(r.get('install_id') == 'install-a' for r in before_recovery))
        b = self.install('b')
        out = self.mod.register([str(b)])
        self.assertEqual(out['results'][0]['status'], 'registered')
        self.assertEqual(os.lstat(reg).st_ino, ino)
        lines = self.registry_lines()
        self.assertTrue(reg.read_bytes().endswith(b'\n'))
        for line in lines:
            json.loads(line)
        self.assertEqual({Path(r['root']).name for r in self.mod.resolve()}, {'a', 'b'})

    def test_middle_corruption_fails_closed(self):
        a, b = self.install('a'), self.install('b')
        self.mod.register([str(a)])
        reg = self.mod.registry_path()
        with open(reg, 'ab') as fh:
            fh.write(b'\x00not-json\n')
        good = reg.read_bytes()
        with open(reg, 'ab') as fh:
            fh.write(json.dumps({'version': 1, 'op': 'upsert', 'root': str(b.resolve()),
                                 'install_id': 'install-b'}).encode() + b'\n')
        before = reg.read_bytes()
        self.assertTrue(before.startswith(good))
        self.assertEqual([r.get('refusal') for r in self.mod.resolve()], ['registry_corrupt'])
        out = self.mod.register([str(self.install('c'))])
        self.assertEqual(out['results'][0]['status'], 'registry_corrupt')
        self.assertEqual(reg.read_bytes(), before)

    def test_register_waits_on_same_inode_lock_no_side_files(self):
        a = self.install('a')
        self.mod.register([str(a)])
        reg = self.mod.registry_path()
        ino = os.lstat(reg).st_ino
        before = reg.read_bytes()
        fd = os.open(reg, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            out = self.mod.register([str(self.install('b'))])
            self.assertEqual(out['results'][0]['status'], 'registry_busy')
            self.assertTrue(out['partial'])
            self.assertEqual(reg.read_bytes(), before)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(os.lstat(reg).st_ino, ino)
        self.assertEqual(sorted(p.name for p in reg.parent.iterdir()), [reg.name])

    def test_status_values_are_fixed(self):
        out = self.mod.register([str(self.work / 'gone'), str(self.install('ok'))])
        for row in out['results']:
            self.assertIn(row['status'], REFUSALS | {'registered', 'duplicate'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
