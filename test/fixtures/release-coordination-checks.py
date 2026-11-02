"""Synthetic release ownership checks; all state lives below the supplied temp HOME."""
import fcntl
from contextlib import nullcontext
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
from release_coordination import PreparedWorker, ReleaseError, coordinate_release
from development_guard import GuardError, Lease

LOCK = 'bundle-auto-upgrade.lock'
PYTHON = sys.executable


class Worker:
    def __init__(self, project, events, *, ready=None, go=None, finish=None):
        self.project, self.events = project, events
        self.callbacks = {'ready': ready, 'go': go, 'finish': finish}
        self.closed = False

    def _step(self, name):
        self.events.append((str(self.project), name))
        callback = self.callbacks[name]
        if callback:
            return callback()

    def ready(self):
        return self._step('ready')

    def go(self):
        return self._step('go')

    def finish(self):
        return self._step('finish') or {
            'project': str(self.project), 'results': [], 'owners': []}

    def close(self):
        self.closed = True
        self.events.append((str(self.project), 'close'))


class ReleaseCoordinationChecks(unittest.TestCase):
    def setUp(self):
        supplied_home = Path(os.environ['HOME'])
        self.assertEqual(supplied_home, supplied_home.resolve(strict=True))
        self.temp = tempfile.TemporaryDirectory(prefix='coordination-', dir=supplied_home)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve(strict=True)
        self.base.chmod(0o700)
        self.projects = []
        self.states = {}
        for name in ('a-project', 'b-project'):
            project = self.base / name
            project.mkdir(mode=0o700)
            states = [project / 'state', project / 'state-companion']
            for state in states:
                state.mkdir(mode=0o700)
            self.projects.append(project)
            self.states[str(project)] = states
        self.events = []

    def roots(self):
        return [state for roots in self.states.values() for state in roots]

    def lock_file(self, state):
        path = state / LOCK
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        return path

    def busy(self, state):
        """A distinct, bounded child competes for the exact named lock."""
        code = '''import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('busy')
else:
    print('free')
finally:
    os.close(fd)
'''
        result = subprocess.run([PYTHON, '-I', '-B', '-c', code, str(state / LOCK)],
                                capture_output=True, text=True, timeout=5,
                                close_fds=True, check=True)
        return result.stdout.strip() == 'busy'

    def assert_locks(self, held):
        for state in self.roots():
            self.assertEqual(self.busy(state), state in held, str(state))

    def assert_refused(self, operation):
        with self.assertRaises(ReleaseError) as raised:
            operation()
        self.assertIsInstance(raised.exception.reason, str)
        self.assertTrue(raised.exception.reason)

    def coordinate(self, install=lambda: None, prepare=None):
        return coordinate_release(self.states, install_both=install,
                                  prepare_worker=prepare or (
                                      lambda project: Worker(project, self.events)))

    def fake_bundle(self, **config):
        root = self.base / ('bundle-' + str(len(list(self.base.glob('bundle-*')))))
        core = root / 'core'
        core.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        (root / 'fixture.json').write_text(json.dumps(config), encoding='utf8')
        (core / 'laya_bundle_upgrade.py').write_text(textwrap.dedent('''\
            import json
            import os
            from pathlib import Path
            import sys
            import time

            ROOT = Path(__file__).resolve().parents[1]
            CONFIG = json.loads((ROOT / 'fixture.json').read_text(encoding='utf8'))
            (ROOT / 'child-pid').write_text(str(os.getpid()), encoding='utf8')
            time.sleep(CONFIG.get('importSleep', 0))
            DEADLINE_SECONDS = 1800

            def _fixture():
                return CONFIG.get('fixtureGuard', False)

            def run(project, *, root=None, home=None):
                project = Path(project)
                (ROOT / 'effect.json').write_text(json.dumps({
                    'project': str(project), 'root': str(root),
                    'isolated': sys.flags.isolated,
                    'noBytecode': sys.dont_write_bytecode,
                    'sentinelOpen': _sentinel_open(),
                }), encoding='utf8')
                time.sleep(CONFIG.get('runSleep', 0))
                return [{'stateRoot': str(project / 'state'),
                         'status': CONFIG.get('status', 'active_upgrade_complete'),
                         'kind': 'active'}]

            def classify(state):
                (ROOT / 'classify-called').write_text(str(state), encoding='utf8')
                return 'active', {'pluginRoot': CONFIG.get('ownerRoot', str(ROOT))}

            def _sentinel_open():
                sentinel = CONFIG.get('sentinel')
                if not sentinel:
                    return False
                identity = os.stat(sentinel)
                for entry in os.listdir('/dev/fd'):
                    try:
                        info = os.fstat(int(entry))
                    except OSError:
                        continue
                    if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                        return True
                return False
            '''), encoding='utf8')
        return root

    def read_ready(self, child):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), 'worker READY timeout')
        self.assertEqual(child.stdout.readline().strip(), 'READY')

    def assert_reaped(self, bundle):
        pid = int((bundle / 'child-pid').read_text(encoding='utf8'))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_01_all_locks_before_single_install_and_nonblocking_competitor(self):
        calls = []

        def install():
            calls.append('install')
            self.assert_locks(set(self.roots()))

        results = self.coordinate(install)
        self.assertEqual(calls, ['install'])
        self.assertEqual([row['project'] for row in results], list(self.states))
        self.assert_locks(set())
        for states in ({}, [], {str(self.projects[0]): []},
                       {str(self.projects[0]): [None]}):
            with self.subTest(invalid_plan=repr(states)):
                self.assert_refused(lambda: coordinate_release(states,
                    install_both=lambda: self.fail('invalid plan installed'),
                    prepare_worker=lambda project: self.fail('invalid plan started')))

    def test_02_busy_foreign_lock_refuses_without_closing_foreign_fd(self):
        state = self.roots()[0]
        fd = os.open(self.lock_file(state), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            calls = []
            self.assert_refused(lambda: self.coordinate(lambda: calls.append('install')))
            self.assertEqual(calls, [])
            os.fstat(fd)
            self.assertTrue(self.busy(state))
        finally:
            os.close(fd)
        self.assertFalse(self.busy(state))

    def test_03_partial_acquire_and_callback_failures_close_only_owned_fds(self):
        for phase in ('partial', 'install', 'nested-lease', 'ready', 'go', 'finish'):
            with self.subTest(phase=phase):
                for state in self.roots():
                    self.lock_file(state)
                identities = {path: (path.stat().st_dev, path.stat().st_ino)
                              for path in (state / LOCK for state in self.roots())}
                foreign = None
                workers = []
                calls = []

                def fail():
                    raise RuntimeError('fixture callback failure')

                def install():
                    calls.append('install')
                    if phase == 'install':
                        fail()
                    if phase == 'nested-lease':
                        with Lease(self.base / 'synthetic-dev-guard'):
                            self.fail('nested development lease must refuse')

                def prepare(project):
                    worker = Worker(project, self.events, **(
                        {phase: fail} if phase in ('ready', 'go', 'finish') else {}))
                    workers.append(worker)
                    return worker

                try:
                    if phase == 'partial':
                        foreign = os.open(self.roots()[1] / LOCK, os.O_RDWR)
                        fcntl.flock(foreign, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    outer = (Lease(self.base / 'synthetic-dev-guard')
                             if phase == 'nested-lease' else nullcontext())
                    with outer:
                        expected = GuardError if phase == 'nested-lease' else Exception
                        with self.assertRaises(expected):
                            self.coordinate(install, prepare)
                        if phase == 'nested-lease':
                            with self.assertRaises(GuardError):
                                with Lease(self.base / 'synthetic-dev-guard'):
                                    self.fail('foreign development lease was closed')
                    self.assertTrue(all(worker.closed for worker in workers))
                    if foreign is not None:
                        self.assertEqual(calls, [])
                        os.fstat(foreign)
                        self.assertTrue(self.busy(self.roots()[1]))
                    self.assertFalse(self.busy(self.roots()[0]))
                finally:
                    if foreign is not None:
                        os.close(foreign)
                for path, identity in identities.items():
                    self.assertEqual((path.stat().st_dev, path.stat().st_ino), identity)
                self.assert_locks(set())

    def test_04_ready_go_sequence_releases_only_current_project(self):
        held = set(self.roots())
        workers = []

        def prepare(project):
            current = self.states[str(project)]

            def ready():
                self.assert_locks(held)

            def go():
                held.difference_update(current)
                self.assert_locks(held)

            def finish():
                self.assert_locks(held)

            worker = Worker(project, self.events, ready=ready, go=go, finish=finish)
            workers.append(worker)
            return worker

        self.coordinate(prepare=prepare)
        expected = [(str(project), step) for project in self.projects
                    for step in ('ready', 'go', 'finish', 'close')]
        self.assertEqual(self.events, expected)
        self.assertTrue(all(worker.closed for worker in workers))

    def test_05_symlinks_hardlinks_and_unsafe_permissions_refuse(self):
        for kind in ('root-symlink', 'lock-symlink', 'lock-hardlink',
                     'root-nul', 'root-unicode',
                     'root-mode', 'lock-mode'):
            with self.subTest(kind=kind):
                area = self.base / kind
                area.mkdir(mode=0o700)
                state = area / 'state'
                state.mkdir(mode=0o700)
                target = area / 'target'
                target.write_text('', encoding='utf8')
                target.chmod(0o600)
                supplied = state
                if kind == 'root-symlink':
                    supplied = area / 'alias'
                    supplied.symlink_to(state, target_is_directory=True)
                elif kind == 'lock-symlink':
                    (state / LOCK).symlink_to(target)
                elif kind == 'lock-hardlink':
                    os.link(target, state / LOCK)
                elif kind == 'root-mode':
                    state.chmod(0o755)
                elif kind == 'root-nul':
                    supplied = Path(str(state) + '\x00bad')
                elif kind == 'root-unicode':
                    supplied = Path(str(state) + '\ud800')
                else:
                    self.lock_file(state).chmod(0o644)
                calls = []
                self.assert_refused(lambda: coordinate_release(
                    {str(self.projects[0]): [supplied]},
                    install_both=lambda: calls.append('install'),
                    prepare_worker=lambda project: Worker(project, self.events)))
                self.assertEqual(calls, [])
                self.assertTrue(target.exists())

    def test_06_root_or_named_lock_inode_replacement_refuses_before_go(self):
        for kind in ('root', 'lock'):
            with self.subTest(kind=kind):
                state = self.roots()[0]
                worker = None

                def ready():
                    if kind == 'root':
                        state.rename(state.with_name('old-state'))
                        state.mkdir(mode=0o700)
                        self.lock_file(state)
                    else:
                        (state / LOCK).rename(state / 'old-lock')
                        self.lock_file(state)

                def prepare(project):
                    nonlocal worker
                    worker = Worker(project, self.events, ready=ready)
                    return worker

                self.events.clear()
                self.assert_refused(lambda: self.coordinate(prepare=prepare))
                self.assertTrue(worker.closed)
                self.assertFalse(any(step == 'go' for _, step in self.events))
                preserved = (state.with_name('old-state') / LOCK
                             if kind == 'root' else state / 'old-lock')
                self.assertTrue(preserved.exists())
                self.assert_locks(set())

    def test_07_coordination_fds_are_not_inherited_by_test_owned_child(self):
        def install():
            identities = [(state / LOCK).stat() for state in self.roots()]
            pairs = [(info.st_dev, info.st_ino) for info in identities]
            code = '''import json, os, sys
expected = {tuple(pair) for pair in json.loads(sys.argv[1])}
leaked = []
for name in os.listdir('/dev/fd'):
    try:
        info = os.fstat(int(name))
    except OSError:
        continue
    if (info.st_dev, info.st_ino) in expected:
        leaked.append(int(name))
print(json.dumps(leaked))
'''
            child = subprocess.run([PYTHON, '-I', '-B', '-c', code, json.dumps(pairs)],
                                   capture_output=True, text=True, timeout=5,
                                   close_fds=False, check=True)
            self.assertEqual(json.loads(child.stdout), [])
        self.coordinate(install)

    def test_08_prepared_worker_ready_before_go_and_validated_finish(self):
        sentinel = self.base / 'sentinel'
        sentinel.write_text('fixture', encoding='utf8')
        fd = os.open(sentinel, os.O_RDONLY)
        os.set_inheritable(fd, True)
        self.addCleanup(os.close, fd)
        for outcome in ('success', 'wrong-owner', 'deferred', 'fixture-guard'):
            with self.subTest(outcome=outcome):
                config = {'sentinel': str(sentinel)}
                if outcome == 'wrong-owner':
                    config['ownerRoot'] = str(self.base / 'another-bundle')
                elif outcome == 'deferred':
                    config['status'] = 'deferred'
                elif outcome == 'fixture-guard':
                    config['fixtureGuard'] = True
                bundle = self.fake_bundle(**config)
                worker = PreparedWorker(bundle, self.projects[0],
                                        ready_seconds=5, run_seconds=5)
                try:
                    if outcome == 'fixture-guard':
                        self.assert_refused(worker.ready)
                        self.assertFalse((bundle / 'effect.json').exists())
                        self.assertFalse((bundle / 'classify-called').exists())
                        continue
                    worker.ready()
                    self.assertFalse((bundle / 'effect.json').exists())
                    self.assertFalse((bundle / 'classify-called').exists())
                    worker.go()
                    if outcome == 'success':
                        result = worker.finish()
                        self.assertEqual(result, {
                            'project': str(self.projects[0]),
                            'results': [{'stateRoot': str(self.projects[0] / 'state'),
                                         'status': 'active_upgrade_complete', 'kind': 'active'}],
                            'owners': [{'stateRoot': str(self.projects[0] / 'state'),
                                        'pluginRoot': str(bundle)}]})
                    else:
                        self.assert_refused(worker.finish)
                finally:
                    worker.close()
                    if outcome == 'fixture-guard':
                        self.assert_reaped(bundle)
                effect = json.loads((bundle / 'effect.json').read_text(encoding='utf8'))
                self.assertEqual(effect['root'], str(bundle))
                self.assertEqual(effect['isolated'], 1)
                self.assertTrue(effect['noBytecode'])
                self.assertFalse(effect['sentinelOpen'])
                self.assertFalse(list(bundle.rglob('__pycache__')))
                self.assert_reaped(bundle)

    def test_09_eof_before_go_cancels_without_upgrade_effect(self):
        bundle = self.fake_bundle()
        child = subprocess.Popen([PYTHON, '-I', '-B',
                                  str(REPO / 'scripts' / 'release_coordination.py'),
                                  '--worker', str(bundle), str(self.projects[0])],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, close_fds=True)
        try:
            self.read_ready(child)
            self.assertFalse((bundle / 'effect.json').exists())
            self.assertFalse((bundle / 'classify-called').exists())
            child.stdin.close()
            child.wait(timeout=5)
            self.assertFalse((bundle / 'effect.json').exists())
            self.assertFalse((bundle / 'classify-called').exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()
        self.assert_reaped(bundle)

    def test_10_ready_and_run_timeout_reap_only_owned_child(self):
        unrelated = subprocess.Popen([PYTHON, '-I', '-B', '-c',
                                      'import time; time.sleep(30)'], close_fds=True)
        try:
            for phase in ('ready', 'run'):
                with self.subTest(phase=phase):
                    bundle = self.fake_bundle(**(
                        {'importSleep': 5} if phase == 'ready' else {'runSleep': 5}))
                    worker = PreparedWorker(bundle, self.projects[0],
                                            ready_seconds=0.5 if phase == 'ready' else 5,
                                            run_seconds=0.5)
                    started = time.monotonic()
                    try:
                        if phase == 'ready':
                            # Confirm the fake import is waiting before timing
                            # READY, so a slow process launch cannot fake a leak.
                            limit = time.monotonic() + 3
                            while not (bundle / 'child-pid').exists():
                                self.assertLess(time.monotonic(), limit)
                                time.sleep(.01)
                            started = time.monotonic()
                            self.assert_refused(worker.ready)
                        else:
                            worker.ready()
                            worker.go()
                            self.assert_refused(worker.finish)
                    finally:
                        worker.close()
                    self.assertLess(time.monotonic() - started, 4)
                    self.assert_reaped(bundle)
                    if phase == 'ready':
                        self.assertFalse((bundle / 'effect.json').exists())
                    self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
