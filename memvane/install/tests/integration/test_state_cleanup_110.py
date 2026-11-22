"""State cleanup: the preview writes nothing; approval removes only what no project selects.

Synthetic install state roots under a fixture HOME. Every check counts
directories and rows; the project's ``.memvane`` folder is compared byte for byte.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sys

from tests.support.case import FixtureCase

from memvane.install import api as install
from memvane.install import state_cleanup
from memvane.kernel import memvane_paths

SELECTED, RELEASED, BUILDING, GONE, MOVED = ('1' * 32, '2' * 32, '3' * 32, '4' * 32, '5' * 32)


@contextmanager
def granted(_store):
    yield object()


@contextmanager
def refused(_store):
    yield None


def tree(root):
    """Every path under ``root`` with its bytes (None for a directory)."""
    found = {}
    for base, directories, files in os.walk(root):
        for name in directories:
            found[os.path.relpath(os.path.join(base, name), root)] = None
        for name in files:
            found[os.path.relpath(os.path.join(base, name), root)] = Path(base, name).read_bytes()
    return found


class CleanupCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = memvane_paths.state_base(self.home)
        self.installs = self.base / 'installs'
        self.installs.mkdir(parents=True, mode=0o700)

    def project(self, name, state=None):
        """A project folder with a wiki card; its pointer names ``state`` when given."""
        root = self.home / name
        (root / '.memvane' / 'wiki').mkdir(parents=True)
        (root / '.memvane' / 'wiki' / 'card.md').write_text('# card\n', encoding='utf8')
        if state is not None:
            (root / '.memvane' / 'route.json').write_text(json.dumps({'stateRoot': str(state)}), encoding='utf8')
        return root

    def state(self, install_id, project=None, *, store=True):
        """An install state root with a store, an arena and (given a project) its route record."""
        root = self.installs / install_id
        (root / 'vector-cache').mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
        (root / 'vector-cache' / 'arena-a.vec').write_bytes(b'v' * 4096)
        if store:
            (root / 'store.sqlite').write_bytes(b's' * 1024)
        if project is not None:
            (root / 'route.json').write_text(json.dumps(
                {'projectRoot': str(project), 'stateRoot': str(root), 'installId': install_id}), encoding='utf8')
        return root

    def layout(self):
        """Selected, released, building, project-gone and re-pointed installs plus a stray entry."""
        self.kept = self.project('kept', self.installs / SELECTED)
        self.state(SELECTED, self.kept)
        self.left = self.project('left')
        self.state(RELEASED, self.left)
        building = self.project('building')
        self.state(BUILDING, building)
        (self.base / 'initial-builds').mkdir(mode=0o700)
        (self.base / 'initial-builds' / ('b' * 32 + '.json')).write_text(json.dumps(
            {'schema': 'memvane-initial-build-pointer-v1', 'projectRoot': str(building),
             'stateRoot': str(self.installs / BUILDING), 'installId': BUILDING}), encoding='utf8')
        self.state(GONE, self.home / 'unmounted')
        moved = self.project('moved', self.installs / SELECTED)
        self.state(MOVED, moved)
        (self.installs / 'notes').mkdir()

    def rows(self, result):
        return {row['install']: (row['action'], row['reason']) for row in result['installs']}


class Released(CleanupCase):
    def test_the_preview_classifies_every_entry_and_writes_nothing(self):
        self.layout()
        before = tree(self.home)
        result = install.state_cleanup(self.home)
        self.assertEqual((result['status'], result['changesApplied']), ('planned', 0))
        self.assertEqual(self.rows(result), {
            SELECTED: ('keep', 'selected'), RELEASED: ('remove', 'released'),
            BUILDING: ('keep', 'building'), GONE: ('keep', 'project_unreachable'),
            MOVED: ('remove', 'released'), 'notes': ('keep', 'unrecognized')})
        sizes = {row['install']: row.get('bytes') for row in result['installs']}
        self.assertEqual((sizes[RELEASED] > 5000, sizes[SELECTED]), (True, None))
        self.assertEqual(tree(self.home), before)

    def test_approval_removes_only_the_released_installs(self):
        self.layout()
        wiki = tree(self.left / '.memvane')
        kept = tree(self.installs / SELECTED)
        result = state_cleanup.cleanup(self.home, approve=True, owner=granted)
        self.assertEqual((result['status'], result['removed'], result['failed'], result['changesApplied']),
                         ('collected', 2, [], 2))
        self.assertGreater(result['bytesFreed'], 10000)
        self.assertEqual(sorted(os.listdir(self.installs)), sorted([SELECTED, BUILDING, GONE, 'notes']))
        # The arena left with its install; the selected install and every project folder are as they were.
        self.assertEqual(tree(self.installs / SELECTED), kept)
        self.assertEqual(tree(self.left / '.memvane'), wiki)
        again = state_cleanup.cleanup(self.home, approve=True, owner=granted)
        self.assertEqual((again['status'], again['removed']), ('collected', 0))

    def test_a_busy_install_is_left_and_reported(self):
        self.layout()
        result = state_cleanup.cleanup(self.home, approve=True, owner=refused)
        self.assertEqual((result['status'], result['removed']), ('partial', 0))
        self.assertEqual(sorted(item['install'] for item in result['failed']), [RELEASED, MOVED])
        self.assertEqual({item['reason'] for item in result['failed']}, {'busy'})
        self.assertTrue((self.installs / RELEASED / 'vector-cache' / 'arena-a.vec').is_file())
        # The install lock held by another owner says the same.
        fd = os.open(self.installs / RELEASED / 'laya-install.lock', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = state_cleanup.cleanup(self.home, approve=True, owner=granted)
        self.assertEqual(result['failed'], [{'install': RELEASED, 'reason': 'busy'}])
        self.assertEqual((result['status'], result['removed']), ('partial', 1))

    def test_an_install_selected_again_before_its_removal_stays(self):
        self.layout()
        pointer = self.left / '.memvane' / 'route.json'

        @contextmanager
        def selecting(_store):
            pointer.write_text(json.dumps({'stateRoot': str(self.installs / RELEASED)}), encoding='utf8')
            yield object()
        result = state_cleanup.cleanup(self.home, approve=True, owner=selecting)
        self.assertIn({'install': RELEASED, 'reason': 'changed'}, result['failed'])
        self.assertTrue((self.installs / RELEASED / 'store.sqlite').is_file())

    def test_an_interrupted_removal_is_finished_by_the_next_run(self):
        self.layout()
        os.rename(self.installs / RELEASED, self.installs / (state_cleanup.REMOVING + RELEASED))
        self.assertEqual(self.rows(install.state_cleanup(self.home))[state_cleanup.REMOVING + RELEASED],
                         ('remove', 'interrupted_removal'))
        result = state_cleanup.cleanup(self.home, approve=True, owner=granted)
        self.assertEqual((result['status'], result['removed']), ('collected', 2))
        self.assertEqual(sorted(os.listdir(self.installs)), sorted([SELECTED, BUILDING, GONE, 'notes']))

    def test_unreadable_records_keep_the_install(self):
        project = self.project('odd')
        self.state(RELEASED, project)
        (project / '.memvane' / 'selection.json').write_text('{not json', encoding='utf8')
        self.assertEqual(self.rows(install.state_cleanup(self.home)), {RELEASED: ('keep', 'pointer_unreadable')})
        (project / '.memvane' / 'selection.json').unlink()
        (self.base / 'initial-builds').mkdir(mode=0o700)
        (self.base / 'initial-builds' / ('c' * 32 + '.json')).write_text('[]', encoding='utf8')
        self.assertEqual(self.rows(install.state_cleanup(self.home)),
                         {RELEASED: ('keep', 'build_pointer_unreadable')})
        (self.base / 'initial-builds' / ('c' * 32 + '.json')).unlink()
        (self.installs / RELEASED / 'route.json').write_text(json.dumps(
            {'projectRoot': str(project), 'stateRoot': str(self.installs / SELECTED)}), encoding='utf8')
        self.assertEqual(self.rows(install.state_cleanup(self.home)), {RELEASED: ('keep', 'project_unreachable')})
        self.assertEqual(state_cleanup.cleanup(self.home, approve=True, owner=granted)['removed'], 0)

    def test_an_install_without_a_store_needs_no_sync_owner(self):
        project = self.project('early')
        self.state(RELEASED, project, store=False)
        result = state_cleanup.cleanup(self.home, approve=True, owner=refused)
        self.assertEqual((result['status'], result['removed']), ('collected', 1))
        self.assertEqual(os.listdir(self.installs), [])

    def test_the_sync_owner_of_the_install_is_taken_for_the_removal(self):
        project = self.project('owned')
        self.state(RELEASED, project)
        seen = []

        @contextmanager
        def recorded(store):
            seen.append((str(store), (self.installs / RELEASED / 'vector-cache').is_dir()))
            yield object()
            seen.append(('released', os.path.lexists(self.installs / RELEASED)))
        result = state_cleanup.cleanup(self.home, approve=True, owner=recorded)
        self.assertEqual(result['removed'], 1)
        # The arena was there when the owner was taken and gone before it was released.
        self.assertEqual(seen, [(str(self.installs / RELEASED / 'store.sqlite'), True), ('released', False)])


    def test_the_real_sync_owner_is_free_for_a_released_install_and_refuses_while_held(self):
        from memvane.runtime import api as runtime
        project = self.project('owned')
        store = self.state(RELEASED, project) / 'store.sqlite'
        with runtime.sync_owner(store, wait=False) as claim:
            self.assertIsNotNone(claim)
            held = install.state_cleanup(self.home, approve=True)
        self.assertEqual((held['status'], held['failed']), ('partial', [{'install': RELEASED, 'reason': 'busy'}]))
        self.assertTrue(store.is_file())
        result = install.state_cleanup(self.home, approve=True)
        self.assertEqual((result['status'], result['removed'], os.listdir(self.installs)), ('collected', 1, []))


class Everything(CleanupCase):
    def roots(self):
        darwin = sys.platform == 'darwin'
        support = (self.home / 'Library' / 'Application Support' / 'memvane' if darwin
                   else self.home / '.local' / 'share' / 'memvane')
        cache = self.home / 'Library' / 'Caches' / 'memvane' if darwin else self.home / '.cache' / 'memvane'
        for path in (support, cache):
            (path / 'v1').mkdir(parents=True)
            (path / 'v1' / 'data').write_bytes(b'd' * 2048)
        config = self.home / '.config' / 'memvane'
        config.mkdir(parents=True)
        (config / 'settings.json').write_text('{}', encoding='utf8')
        return self.base.parent, support, cache, config

    def test_the_preview_lists_the_roots_and_writes_nothing(self):
        project = self.project('kept', self.installs / SELECTED)
        self.state(SELECTED, project)
        self.roots()
        before = tree(self.home)
        result = install.state_cleanup_everything(self.home)
        self.assertEqual((result['status'], result['scope'], result['changesApplied']), ('planned', 'everything', 0))
        self.assertEqual([(row['root'], row['action']) for row in result['roots']],
                         [('state', 'remove'), ('support', 'remove'), ('cache', 'remove')])
        self.assertEqual(tree(self.home), before)

    def test_approval_removes_the_roots_and_never_a_project_folder_or_the_settings(self):
        project = self.project('kept', self.installs / SELECTED)
        self.state(SELECTED, project)
        state, support, cache, config = self.roots()
        folder, settings = tree(project), tree(config)
        result = install.state_cleanup_everything(self.home, approve=True)
        self.assertEqual((result['status'], result['removed'], result['failed']), ('collected', 3, []))
        self.assertEqual([os.path.lexists(path) for path in (state, support, cache)], [False] * 3)
        self.assertEqual([name for name in os.listdir(state.parent) if 'memvane' in name], [])
        self.assertEqual((tree(project), tree(config)), (folder, settings))
        again = install.state_cleanup_everything(self.home, approve=True)
        self.assertEqual((again['status'], again['roots']), ('collected', []))

    def test_a_root_that_is_a_link_is_never_followed(self):
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'keep').write_text('keep', encoding='utf8')
        state = self.base.parent
        os.rename(state, self.home / 'real-state')
        os.symlink(elsewhere, state)
        result = install.state_cleanup_everything(self.home, approve=True)
        self.assertEqual((result['status'], result['removed']), ('partial', 0))
        self.assertEqual(result['failed'], [{'root': 'state', 'reason': 'unsafe_root'}])
        self.assertEqual((elsewhere / 'keep').read_text(encoding='utf8'), 'keep')
        self.assertTrue(os.path.islink(state))
