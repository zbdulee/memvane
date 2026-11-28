"""Overview checkout states over registry rows written by the registry's own writers.

Every state here is written by ``registry.api.after_setup`` (setup's
registration) and ``registry.api.reconcile`` observing real file and route
changes: a removed or deleted worktree, a moved-away root, a replaced root
inode, an origin restore and a broken route. The one exception is the other
claimant of a contested root (see ``claim``); its ``conflict`` status is still
written by setup's registration. No DTO is built by hand.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sqlite3
import time

from memvane.registry import api as registry
from memvane.ui import read_adapters
from memvane.ui.types import target_id
from tests.support.case import FixtureCase

DAY = 24 * 3600
FOREIGN = 'e' * 32


class OverviewStateTests(FixtureCase):
    def onboard(self, name, install_id, *, worktrees=(), dirname=None, git=True, register=True):
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        project = make_project(self.home, name=name, dirname=dirname or name, git=git)
        project.write_note('notes.md', f'Notes of {name}.\n')
        project.commit()
        linked = [project.linked_worktree(wt, f'{wt}-branch') for wt in worktrees]
        install = build_install(self, project, install_id=install_id)
        if not register:
            return project, install, linked
        outcome = registry.after_setup('activate-index-origin', str(project.root), str(install.state),
                                       {'status': 'selected', 'phase': 'selected'})
        self.assertIn(outcome['status'], ('registered', 'unchanged'), outcome)
        return project, install, linked

    def claim(self, install_id, state_root, root):
        from memvane.kernel import memvane_paths
        path = memvane_paths.state_base(self.home) / 'auto-context' / 'registry' / 'projects.sqlite'
        with contextlib.closing(sqlite3.connect(str(path))) as db:
            db.execute("INSERT INTO entries(install_id,state_root,project_root,target_root,kind,status,"
                       "observed_at,version) VALUES (?,?,?,?,'main','active',1,1)",
                       (install_id, str(state_root), str(root), str(root)))
            db.commit()

    def reconcile(self, now):
        result = registry.reconcile(now=now, backfill_page=0, seconds=60.0)
        self.assertEqual(result['status'], 'ok', result)

    def rows(self):
        snapshot = registry.snapshot(home=self.home, reconcile=False)
        return {(row['installId'], row['targetRoot']): row for row in snapshot['rows']}

    def nodes(self, view):
        found = {}
        for group in view['projects']:
            for node in (group['main'], *group['worktrees']):
                found[node['targetId']] = node
        return found

    def test_registry_states_map_to_view_states(self):
        live, live_install, (ready_wt, gone_wt, pruned_wt) = self.onboard(
            'live', '1' * 32, worktrees=('ready', 'gone', 'pruned'))
        pending, pending_install, _ = self.onboard('pending', '2' * 32)
        missing, missing_install, _ = self.onboard('missing', '3' * 32)
        released, released_install, _ = self.onboard('released', '4' * 32)
        moved, moved_install, _ = self.onboard('moved', '5' * 32)
        broken, broken_install, _ = self.onboard('broken', '6' * 32)
        contested, contested_install, _ = self.onboard('contested', '7' * 32, register=False)
        # A worktree under a directory that later denies access: its root cannot be observed.
        lockdir = self.home / 'lockdir'
        lockdir.mkdir()
        locked_root = lockdir / 'locked'
        live.git('worktree', 'add', '-q', '-b', 'locked-branch', str(locked_root))
        outcome = registry.after_setup('activate-index-origin', str(live.root), str(live_install.state),
                                       {'status': 'selected', 'phase': 'selected'})
        self.assertIn(outcome['status'], ('registered', 'unchanged'), outcome)

        # Two installs claim one root. Onboarding refuses a second selected install
        # at a selected root (``project_selection_already_exists``), so the other
        # claimant is one row inserted the way the registry's own conflict test
        # does; setup's registration then writes ``conflict`` on both rows.
        self.claim(FOREIGN, self.home / 'gone-state', contested.root)
        outcome = registry.after_setup('activate-index-origin', str(contested.root), str(contested_install.state),
                                       {'status': 'selected', 'phase': 'selected'})
        self.assertEqual(outcome['status'], 'conflict', outcome)

        # A worktree git no longer lists is released; one whose directory is gone
        # while git still lists it stays an active row with no root.
        live.git('worktree', 'remove', '--force', str(pruned_wt.root))
        shutil.rmtree(gone_wt.root)
        # The root disappears: observed once (pending), or twice a day apart (detached/missing).
        missing.root.rename(missing.root.with_name('missing-away'))
        # The origin selection is restored: a proved release.
        self.assertEqual(released_install.restore()['status'], 'restored')
        # The root is replaced by a copy (another inode).
        copy = moved.root.with_name('moved-copy')
        shutil.copytree(moved.root, copy, symlinks=True)
        shutil.rmtree(moved.root)
        copy.rename(moved.root)
        # The route no longer proves the install, the root stays.
        route = broken.root / '.memvane' / 'route.json'
        saved_route = route.read_bytes()
        os.chmod(route, 0o600)
        route.write_bytes(b'{"schema":')
        now = time.time()
        self.reconcile(now)
        self.reconcile(now + DAY + 3600)
        pending.root.rename(pending.root.with_name('pending-away'))
        self.reconcile(now + DAY + 7200)
        rows = self.rows()

        def row(install, root):
            return rows[(getattr(install, 'install_id', install), str(root))]

        self.assertEqual({key: (row(*key)['status'], row(*key)['reason']) for key in (
            (live_install, live.root), (live_install, ready_wt.root), (live_install, gone_wt.root),
            (live_install, pruned_wt.root), (live_install, locked_root), (pending_install, pending.root),
            (missing_install, missing.root),
            (released_install, released.root), (moved_install, moved.root), (broken_install, broken.root),
            (contested_install, contested.root), (FOREIGN, contested.root))}, {
            (live_install, live.root): ('active', None),
            (live_install, ready_wt.root): ('active', None),
            (live_install, gone_wt.root): ('active', None),
            (live_install, pruned_wt.root): ('detached', 'released'),
            (live_install, locked_root): ('active', None),
            (pending_install, pending.root): ('missing_pending', None),
            (missing_install, missing.root): ('detached', 'missing'),
            (released_install, released.root): ('detached', 'released'),
            (moved_install, moved.root): ('detached', 'root_moved'),
            (broken_install, broken.root): ('active', None),
            (contested_install, contested.root): ('conflict', None),
            (FOREIGN, contested.root): ('conflict', None),
        })
        self.assertTrue(row(broken_install, broken.root)['proof'].startswith('unproved'))

        # Past ``max_installs`` no live proof runs: the registry's recorded proof decides.
        capped = self.nodes(read_adapters.overview(home=self.home, max_installs=0))
        recorded = row(broken_install, broken.root)['proof'].split(':', 1)[1]
        self.assertEqual((capped[broken_install.install_id]['viewState'],
                          capped[broken_install.install_id]['stateReason']), ('proof_failed', recorded))
        self.assertEqual((capped[live_install.install_id]['viewState'],
                          capped[live_install.install_id]['stateReason']), ('ready', None))

        os.chmod(lockdir, 0)
        try:
            view = read_adapters.overview(home=self.home)
        finally:
            os.chmod(lockdir, 0o700)
        nodes = self.nodes(view)

        def node(install, root, kind='main'):
            return nodes[target_id(getattr(install, 'install_id', install), str(root), kind)]

        expected = {
            ('live', 'main'): (node(live_install, live.root), 'ready', 'main'),
            ('ready', 'worktree'): (node(live_install, ready_wt.root, 'worktree'), 'ready', 'ready-branch'),
            ('gone', 'worktree'): (node(live_install, gone_wt.root, 'worktree'), 'path_missing', 'unknown'),
            ('pruned', 'worktree'): (node(live_install, pruned_wt.root, 'worktree'), 'released', 'unknown'),
            ('locked', 'worktree'): (node(live_install, locked_root, 'worktree'), 'unreadable', 'unknown'),
            ('pending', 'main'): (node(pending_install, pending.root), 'path_missing', 'unknown'),
            ('missing', 'main'): (node(missing_install, missing.root), 'path_missing', 'unknown'),
            ('released', 'main'): (node(released_install, released.root), 'released', 'unknown'),
            ('moved', 'main'): (node(moved_install, moved.root), 'moved', 'unknown'),
            ('broken', 'main'): (node(broken_install, broken.root), 'proof_failed', 'main'),
            ('contested', 'main'): (node(contested_install, contested.root), 'conflict', 'unknown'),
            ('foreign', 'main'): (node(FOREIGN, contested.root), 'conflict', 'unknown'),
        }
        for key, (found, state, branch) in expected.items():
            self.assertEqual((found['viewState'], found['branch']), (state, branch), key)
            self.assertNotEqual(found['branch'], 'non-git', key)
        broken_node = node(broken_install, broken.root)
        self.assertTrue(broken_node['stateReason'].startswith('route_'), broken_node['stateReason'])
        self.assertEqual(node(live_install, locked_root, 'worktree')['stateReason'], 'EACCES')
        self.assertEqual([found['stateReason'] for key, (found, _, _) in expected.items()
                          if key not in (('broken', 'main'), ('locked', 'worktree'))
                          and found['stateReason'] is not None], [])
        # The SLO stays on the install line; a failed proof has no digest.
        self.assertEqual(broken_node['slo'], 'no-data')
        self.assertEqual(len(nodes), len(expected))

        # A live proof that succeeds outranks the proof the registry recorded earlier.
        route.write_bytes(saved_route)
        self.assertTrue(self.rows()[(broken_install.install_id, str(broken.root))]['proof'].startswith('unproved'))
        healed = self.nodes(read_adapters.overview(home=self.home))[broken_install.install_id]
        self.assertEqual((healed['viewState'], healed['stateReason'], healed['branch']), ('ready', None, 'main'))

    def test_a_project_folder_inside_a_repository_reads_the_repository_branch(self):
        from tests.support.project import Project
        mono = self.home / 'mono'
        mono.mkdir()
        repo = Project(mono, 'mono', ())
        repo.git('init', '-q')
        (mono / 'README.md').write_text('mono\n', encoding='utf-8')
        repo.commit()
        project, install, _ = self.onboard('sub', '9' * 32, dirname='mono/sub', git=False)
        view = read_adapters.overview(home=self.home)
        main = self.nodes(view)[install.install_id]
        self.assertEqual((main['viewState'], main['branch'], main['displayRoot']),
                         ('ready', 'main', str(project.root)))
        self.assertFalse((project.root / '.git').exists())
        self.assertEqual(Path(main['displayRoot']).parent, mono)


class ViewStateMappingTests(FixtureCase):
    """Combinations the registry DDL does not allow today still read as ``unknown``, never hidden."""

    def test_an_unknown_status_or_reason_is_unknown_with_its_values(self):
        for status, reason in (('detached', None), ('archived', None), ('missing_pending', 'released'),
                               ('conflict', 'missing')):
            with self.subTest(status=status, reason=reason):
                self.assertEqual(read_adapters._view_state({'status': status, 'reason': reason}),
                                 ('unknown', f'{status}/{reason}'))
