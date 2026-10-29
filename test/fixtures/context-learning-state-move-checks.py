#!/usr/bin/env python3
"""Learning state move and automatic selector heal, synthetic HOME only.

The move runs in-process against a temporary HOME passed explicitly. The heal
runs in a child process whose HOME is the temporary one and whose code is a
copy of ``core`` inside synthetic official plugin caches; the model, the
synthetic attestation run and inference preparation are stubbed at the two
heal seams (``selector_heal.attest``/``prepare``) and at the prediction
transport, like ``context-learning-adapter-binding-checks.py``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'core'))
from context_learning import state_move  # noqa: E402
from context_learning.runtime_setup import learning_state_base  # noqa: E402

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL = [REAL_HOME / 'Library' / 'Application Support' / name
        for name in ('qmd-auto-context', 'memvane')]


def _mtimes():
    return {str(path): path.stat().st_mtime_ns if path.exists() else None for path in REAL}


BEFORE = _mtimes()
RETRY = 'learning-state-move-retry.json'


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class Kill(BaseException):
    pass


def hold(path):
    """An exclusive flock on ``path`` (created private), or BlockingIOError."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


class FakeResident:
    """The per-user resident service as the mover sees it.

    It answers ``hello`` and ``drain`` on ``control.sock`` (the socket the
    embedding clients reach the service through), holds the service lease
    and, best effort like the hosted learning inference, the learning lanes
    it would hold, recording its PID in the learning ``service.pid.json``.
    ``drain`` closes the control socket at once and releases the locks
    ``linger`` seconds later (the real service closes its socket before its
    lease).
    """
    LANES = ('service-lease.lock', 'cpu-share-0.lock', 'runtime-monitor.lock')

    def __init__(self, service, inference, *, linger=0):
        import laya_resident_service as svc
        self.svc, self.service, self.inference = svc, service, Path(inference)
        self.commands = []
        self.linger = linger
        self.fds = [hold(service / svc.LEASE)]
        for name in self.LANES:
            try:
                self.fds.append(hold(self.inference / name))
            except OSError:
                pass  # busy, or the moved-away old root
        try:
            private(self.inference / 'service.pid.json', {'pid': os.getpid()})
        except OSError:
            pass
        self.path = service / svc.CONTROL
        if self.path.exists():
            self.path.unlink()
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen(8)
        self.server.settimeout(.02)
        self.alive = True
        self.inflight = 0
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while self.alive:
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                try:
                    row = self.svc._read_line(conn, time.monotonic() + 1)
                    self.commands.append(row.get('command'))
                    if row.get('command') == 'drain':
                        self.svc._write_line(conn, {'status': 'draining'})
                        self.stop(linger=self.linger)
                    else:
                        self.svc._write_line(conn, {'status': 'ready', 'protocol': 1,
                                                    'protocols': [1], 'pid': os.getpid(),
                                                    'inflight': self.inflight})
                except (OSError, ValueError):
                    continue

    def stop(self, linger=0):
        if not self.alive:
            return
        self.alive = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.server.close()
        fds, self.fds = self.fds, []

        def release():
            for fd in fds:
                os.close(fd)
        if linger:
            threading.Timer(linger, release).start()
        else:
            release()

    def serving(self):
        return self.svc.hello(self.service) is not None


class FakeSpawn:
    """``ensure_service``'s spawn: a successor that keeps its inherited
    ``successor.lock`` until it holds the service lease, then serves the
    learning root named in its argv."""

    def __init__(self):
        self.started = []

    def __call__(self, argv, *, pass_fds):
        successor = os.dup(pass_fds[0])
        service, learning = Path(argv[5]), Path(argv[6])

        def run():
            try:
                while True:
                    try:
                        fake = FakeResident(service, learning)
                        break
                    except BlockingIOError:
                        time.sleep(.02)
                self.started.append((fake, str(learning)))
            finally:
                os.close(successor)
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        for fake, _root in self.started:
            fake.stop()


class LearningMoveTests(unittest.TestCase):
    def setUp(self):
        # /tmp: the resident service's control socket path stays below the
        # AF_UNIX limit.
        temp = tempfile.TemporaryDirectory(prefix='lsm-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        support = (('Library', 'Application Support') if sys.platform == 'darwin'
                   else ('.local', 'share'))
        self.legacy = self.home.joinpath(*support, 'qmd-auto-context')
        self.current = self.home.joinpath(*support, 'memvane')
        self.namespace = self.legacy / 'live-selection' / ('a' * 64)
        self.namespace.mkdir(parents=True, mode=0o700)
        for path in (self.legacy, self.legacy / 'live-selection'):
            path.chmod(0o700)
        artifact = self.namespace / 'laya-base-x.artifact'
        artifact.write_bytes(b'head')
        artifact.chmod(0o600)
        private(self.namespace / 'active-checkpoint.json',
                {'schema_version': 1, 'artifact_path': str(artifact),
                 'artifact_sha256': hashlib.sha256(b'head').hexdigest()})
        self.inference = self.legacy / 'laya-inference'
        self.inference.mkdir(mode=0o700)
        for name in ('provider.lock', 'compute.lock', 'runtime-worker.lock'):
            (self.inference / name).write_bytes(b'')
            (self.inference / name).chmod(0o600)
        self.lock_inode = (self.inference / 'provider.lock').stat().st_ino

    def retry_now(self):
        (self.current / RETRY).unlink(missing_ok=True)

    def service_dir(self):
        service = self.home / '.local' / 'state' / 'memvane' / 'service'
        service.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (service, service.parent, service.parent.parent):
            path.chmod(0o700)
        return service

    def resident(self, linger=0):
        fake = FakeResident(self.service_dir(), self.inference, linger=linger)
        self.addCleanup(fake.stop)
        return fake

    def restarts(self):
        """The real restart path (``ensure_service``) with a synthetic spawn."""
        spawn = FakeSpawn()
        patcher = mock.patch.object(state_move, '_service_spawn', spawn, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(spawn.stop)
        return spawn.started

    def test_a_held_lane_defers_without_draining_the_embedding_service(self):
        # The hosted inference and the embedding host are one process: a lane
        # that defers the move must be found before the service is drained.
        fake = self.resident()
        restarts = self.restarts()
        for name, reason in (('compute.lock', 'learning_lane_busy'),
                             ('runtime-worker.lock', 'learning_runtime_worker_busy')):
            fd = hold(self.inference / name)
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(state_move.LearningMoveDeferred, reason):
                    state_move.move(self.home, wait=0.5)
                self.assertLess(time.monotonic() - started, 3)
            finally:
                os.close(fd)
            self.assertNotIn('drain', fake.commands)
            self.assertTrue(fake.serving())
            self.retry_now()
        self.assertEqual(restarts, [])
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_service_serving_a_request_or_a_spent_budget_is_not_drained(self):
        fake = self.resident()
        restarts = self.restarts()
        fake.inflight = 1
        with self.assertRaisesRegex(state_move.LearningMoveDeferred, 'learning_service_busy'):
            state_move.move(self.home, wait=2)
        self.retry_now()
        fake.inflight = 0
        # The runtime worker's turn used the budget up to the drain reserve.
        real = state_move._wait_lock

        def slow(path, deadline):
            time.sleep(1.1)
            return real(path, deadline)
        with mock.patch.object(state_move, '_wait_lock', slow):
            with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                        'learning_service_budget'):
                state_move.move(self.home, wait=2)
        self.assertNotIn('drain', fake.commands)
        self.assertTrue(fake.serving())
        self.assertEqual(restarts, [])
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_started_move_resumes_without_waiting_for_the_backoff(self):
        def die(name):
            if name == 'moved:live-selection':
                raise Kill(name)
        with mock.patch.object(state_move, '_step', die):
            with self.assertRaises(Kill):
                state_move.move(self.home, wait=0.2)
        fd = hold(self.inference / 'compute.lock')
        try:
            with self.assertRaisesRegex(state_move.LearningMoveDeferred, 'learning_lane_busy'):
                state_move.move(self.home, wait=0.2)
        finally:
            os.close(fd)
        self.assertTrue((self.current / RETRY).exists())
        self.assertEqual(state_move.move(self.home, wait=0.2)['status'], 'learning_state_moved')

    def test_a_respawning_service_and_runtime_worker_are_quiesced_and_the_move_completes(self):
        # Live shape: the runtime wake worker re-takes its lease right after
        # each bounded drain (self-rewake), and its sync wakes start the
        # resident service again whenever none runs and no successor waits.
        service = self.service_dir()
        residents = [self.resident()]
        restarts = self.restarts()
        done = threading.Event()
        cycles = []

        def runtime_worker():
            while not done.is_set():
                try:
                    fd = hold(self.inference / 'runtime-worker.lock')
                except BlockingIOError:
                    time.sleep(.005)
                    continue
                except OSError:
                    return  # the lane moved: this worker's root is gone
                cycles.append(1)
                time.sleep(.3)
                os.close(fd)
                time.sleep(.005)

        import laya_resident_service as svc

        def respawn():
            while not done.is_set():
                if not residents[-1].alive and svc.hello(service) is None:
                    try:
                        successor = hold(service / svc.SUCCESSOR)
                    except BlockingIOError:
                        successor = None
                    if successor is not None:
                        try:
                            fake = FakeResident(service, self.inference)
                            self.addCleanup(fake.stop)
                            residents.append(fake)
                        except BlockingIOError:
                            pass
                        finally:
                            os.close(successor)
                time.sleep(.02)
        threads = [threading.Thread(target=runtime_worker, daemon=True),
                   threading.Thread(target=respawn, daemon=True)]
        for thread in threads:
            thread.start()
        try:
            while not cycles:
                time.sleep(.01)
            result = state_move.move(self.home, wait=10)
            serving = svc.hello(service) is not None
        finally:
            done.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(result['status'], 'learning_state_moved', result)
        self.assertEqual(learning_state_base(self.home), self.current)
        # One drain, and the service is started again right after the move.
        self.assertEqual(sum(fake.commands.count('drain') for fake in residents), 1)
        # The move does not return before the service answers again.
        self.assertEqual(result['residentRestart'], 'restarted', result)
        self.assertTrue(serving)
        self.assertFalse((self.current / RETRY).exists())

    def test_a_drained_service_still_holding_its_lease_is_restarted_without_a_hook(self):
        # The real service closes its control socket before it releases its
        # lease: one ensure_service call then elects no successor.
        import laya_resident_service as svc
        service = self.service_dir()
        moved_root = str(self.current / 'laya-inference')
        cases = (('moved', 1.5, 10, None), ('inline', 3, 2, None), ('detached', 3, 2, .3))
        for name, linger, wait, restart_wait in cases:
            with self.subTest(name):
                if name != 'moved':
                    self.setUp()
                    service = self.service_dir()
                old = self.resident(linger=linger)
                restarts = self.restarts()
                detached = []

                def detach(home, events):
                    thread = threading.Thread(target=state_move.restarter,
                                              args=(home, events, 15), daemon=True)
                    detached.append(thread)
                    thread.start()
                with mock.patch.object(state_move, 'RESTART_WAIT_SECONDS', restart_wait or getattr(
                            state_move, 'RESTART_WAIT_SECONDS', 10), create=True), \
                        mock.patch.object(state_move, '_detach', detach, create=True):
                    if name == 'moved':
                        outcome = state_move.move(self.home, wait=wait)['residentRestart']
                    else:
                        with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                                    'learning_service_draining') as caught:
                            state_move.move(self.home, wait=wait)
                        outcome = caught.exception.restart
                    if name == 'detached':
                        self.assertEqual(outcome, 'restarter_started')
                        detached[0].join(timeout=15)
                    else:
                        self.assertEqual(outcome, 'restarted')
                self.assertEqual(old.commands.count('drain'), 1)
                self.assertFalse(old.alive)
                self.assertIsNotNone(svc.hello(service))
                self.assertEqual(len(restarts), 1, restarts)
                if name == 'moved':
                    self.assertEqual(restarts[0][1], moved_root)

    def test_an_unanswering_learning_service_defers_before_the_drain(self):
        # A learning-only process of an earlier bundle holds the inference
        # lease and socket but gives no valid version answer: unknown.
        from context_learning import inference_service as learning
        for reply in (None, b'not json\n'):
            with self.subTest(reply=reply):
                # The learning-only process holds the lease before the
                # resident's hosted inference could.
                lease = hold(self.inference / learning.LEASE_NAME)
                path = self.inference / learning.SOCKET_NAME
                server = socket.socket(socket.AF_UNIX)
                try:
                    fake = self.resident()
                    private(self.inference / 'service.pid.json', {'pid': 999999})
                    server.bind(str(path))
                    os.chmod(path, 0o600)
                    server.listen(4)

                    def answer():
                        try:
                            conn, _ = server.accept()
                            conn.recv(4096)
                            conn.sendall(reply)
                            conn.close()
                        except OSError:
                            pass
                    if reply is not None:
                        threading.Thread(target=answer, daemon=True).start()
                    with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                                'learning_inference_unknown'):
                        state_move.move(self.home, wait=2)
                finally:
                    server.close()
                    os.close(lease)
                    path.unlink(missing_ok=True)
                self.assertNotIn('drain', fake.commands)
                self.assertTrue(fake.serving())
                fake.stop()
                self.retry_now()

    def test_a_deferred_worker_run_returns_promptly_and_backs_off(self):
        import laya_bundle_upgrade as upgrade
        from context_learning import selector_heal
        fake = self.resident()
        restarts = self.restarts()
        project = self.home / 'project'
        project.mkdir()
        heals = []
        fd = hold(self.inference / 'runtime-worker.lock')
        try:
            with mock.patch.object(upgrade, '_project_states', return_value=[]), \
                    mock.patch.object(upgrade, '_collect_shared'), \
                    mock.patch.object(state_move, 'BUDGET_SECONDS', 1.0, create=True), \
                    mock.patch.object(selector_heal, 'heal',
                                      side_effect=lambda *a, **k: heals.append(a)):
                for expected, bound in (('learning_runtime_worker_busy', 4),
                                        ('learning_state_move_backoff', 1)):
                    started = time.monotonic()
                    rows = upgrade.run(project, home=self.home)
                    self.assertLess(time.monotonic() - started, bound)
                    self.assertNotIn('drain', fake.commands)
                    self.assertEqual(rows[-1]['learningState'], expected, rows)
        finally:
            os.close(fd)
        # The pending move never drained the embedding service, nor healed.
        self.assertNotIn('drain', fake.commands)
        self.assertTrue(fake.serving())
        self.assertEqual(heals, [])
        self.assertEqual(restarts, [])

    def test_deferrals_back_off_exponentially_per_user(self):
        clock = [time.time_ns()]
        waits = []
        fd = hold(self.inference / 'compute.lock')
        try:
            with mock.patch.object(state_move, '_now_ns', lambda: clock[0], create=True):
                for _attempt in range(7):
                    with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                                'learning_lane_busy'):
                        state_move.move(self.home, wait=0.2)
                    retry = json.loads((self.current / RETRY).read_text())
                    waits.append((retry['nextNs'] - clock[0]) // 10**9)
                    # Before it is due, a run defers at once and takes no lock.
                    clock[0] += 60 * 10**9
                    with mock.patch.object(state_move, '_lock',
                                           side_effect=AssertionError('lock taken')):
                        with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                                    'learning_state_move_backoff'):
                            state_move.move(self.home, wait=0.2)
                    clock[0] = retry['nextNs']
                self.assertEqual(waits, [120, 240, 480, 960, 1920, 3600, 3600])
                os.close(fd)
                fd = None
                self.assertEqual(state_move.move(self.home, wait=0.2)['status'],
                                 'learning_state_moved')
        finally:
            if fd is not None:
                os.close(fd)
        self.assertFalse((self.current / RETRY).exists())

    def test_the_learning_move_never_runs_inline_in_a_hook(self):
        # Only the detached bundle upgrade worker (and the owner's command)
        # runs the move; a hook only records a request.
        allowed = {'core/laya_bundle_upgrade.py', 'core/context_learning/state_move.py'}
        for path in sorted([*SOURCE.glob('core/**/*.py'), *SOURCE.glob('core/**/*.mjs'),
                            *SOURCE.glob('hooks/**/*')]):
            if not path.is_file() or str(path.relative_to(SOURCE)) in allowed:
                continue
            text = path.read_text(errors='replace')
            for needle in ('state_move.move(', '_learning_upkeep(',
                           'laya_bundle_upgrade.run(', 'upgrade.run('):
                self.assertNotIn(needle, text, str(path))

    def test_move_switches_both_trees_and_rebases_the_checkpoint(self):
        self.assertEqual(learning_state_base(self.home), self.legacy)
        result = state_move.move(self.home, wait=0.2)
        self.assertEqual(result['status'], 'learning_state_moved', result)
        self.assertEqual(learning_state_base(self.home), self.current)
        # Read-only regular files stand where the trees were.
        for name in ('live-selection', 'laya-inference'):
            self.assertTrue((self.legacy / name).is_file())
            self.assertTrue(state_move.moved_sentinel(self.legacy / name))
        moved = self.current / 'live-selection' / ('a' * 64)
        pointer = json.loads((moved / 'active-checkpoint.json').read_text())
        self.assertEqual(pointer['artifact_path'], str(moved / 'laya-base-x.artifact'))
        # Lane locks keep their inode: a holder from before the move and one
        # from after it exclude each other.
        self.assertEqual((self.current / 'laya-inference' / 'provider.lock').stat().st_ino,
                         self.lock_inode)
        from context_learning.inference_service import service_root
        self.assertEqual(service_root(create=True, home=self.home),
                         self.current / 'laya-inference')
        self.assertEqual(state_move.move(self.home)['status'], 'learning_state_switched')

    def test_a_handoff_contender_is_waited_for_and_workers_are_asked_to_yield(self):
        # A handoff contender holds runtime-worker-handoff.lock only until it
        # holds the worker lease; the mover waits for it as a kernel waiter
        # instead of deferring at once, and asks workers to yield meanwhile.
        handoff = self.inference / 'runtime-worker-handoff.lock'
        fd = hold(handoff)
        marker = self.inference / 'runtime-worker-yield.json'
        seen = []

        def release():
            seen.append(marker.is_file())
            if marker.is_file():
                until = json.loads(marker.read_text())['until']
                seen.append(time.time() < until <= time.time() + 5)
                seen.append(oct(marker.stat().st_mode & 0o777))
            os.close(fd)
        timer = threading.Timer(.3, release)
        timer.start()
        try:
            started = time.monotonic()
            result = state_move.move(self.home, wait=3)
        finally:
            timer.join()
        self.assertEqual(result['status'], 'learning_state_moved', result)
        self.assertGreaterEqual(time.monotonic() - started, .25)
        self.assertEqual(seen, [True, True, '0o600'])
        # The request ends with the wait: nothing is left in either tree.
        self.assertFalse((self.current / 'laya-inference' / 'runtime-worker-yield.json').exists())
        self.assertEqual(learning_state_base(self.home), self.current)

    def test_a_sleeping_wake_timer_does_not_defer_the_move(self):
        # The wake queue's timer holds only its own lock while it sleeps
        # toward the next due wake; it is not a lane.
        fd = hold(self.inference / 'runtime-wake-timer.lock')
        try:
            result = state_move.move(self.home, wait=1)
        finally:
            os.close(fd)
        self.assertEqual(result['status'], 'learning_state_moved', result)

    def test_a_busy_lane_defers_before_anything_moves(self):
        fd = os.open(self.inference / 'runtime-worker.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                        'learning_runtime_worker_busy'):
                state_move.move(self.home, wait=0.2)
        finally:
            os.close(fd)
        self.assertTrue((self.legacy / 'live-selection').is_dir())
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_service_still_holding_its_lease_defers_the_move(self):
        service = self.home / '.local' / 'state' / 'memvane' / 'service'
        service.mkdir(parents=True, mode=0o700)
        for path in (service.parent, service.parent.parent, service.parent.parent.parent):
            path.chmod(0o700)
        (service / 'service.lock').write_bytes(b'')
        (service / 'service.lock').chmod(0o600)
        fd = os.open(service / 'service.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                        'learning_service_draining'):
                state_move.move(self.home, wait=0.3)
        finally:
            os.close(fd)
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_setup_bound_namespace_is_not_moved(self):
        private(self.namespace / state_move.SETUP_BINDINGS, {'phase': 'bound'})
        with self.assertRaisesRegex(state_move.LearningMoveRefused,
                                    'learning_state_move_bound'):
            state_move.move(self.home, wait=0.2)
        self.assertEqual(learning_state_base(self.home), self.legacy)

    def test_a_crash_between_the_renames_resumes(self):
        def die(name):
            if name == 'moved:live-selection':
                raise Kill(name)
        with mock.patch.object(state_move, '_step', die):
            with self.assertRaises(Kill):
                state_move.move(self.home, wait=0.2)
        self.assertEqual(state_move.status(self.home)[0], 'pending')
        result = state_move.move(self.home, wait=0.2)
        self.assertEqual(result['status'], 'learning_state_moved')
        self.assertEqual(learning_state_base(self.home), self.current)

    def test_a_stop_after_the_switch_still_fixes_the_checkpoints(self):
        def die(name):
            if name == 'switched':
                raise Kill(name)
        with mock.patch.object(state_move, '_step', die):
            with self.assertRaises(Kill):
                state_move.move(self.home, wait=0.2)
        self.assertEqual(state_move.status(self.home)[0], 'pending')
        state_move.move(self.home, wait=0.2)
        moved = self.current / 'live-selection' / ('a' * 64)
        pointer = json.loads((moved / 'active-checkpoint.json').read_text())
        self.assertEqual(pointer['artifact_path'], str(moved / 'laya-base-x.artifact'))
        self.assertEqual(state_move.status(self.home)[0], 'switched')

    def test_a_stale_process_cannot_recreate_the_old_lane_or_selection_tree(self):
        # An earlier bundle's service_root resolved the old root, then paused
        # before its mkdir while the move ran.
        old_lane = learning_state_base(self.home) / 'laya-inference'
        old_selection = learning_state_base(self.home) / 'live-selection'
        self.assertEqual(old_lane, self.inference)
        state_move.move(self.home, wait=0.2)
        with self.assertRaises(OSError):
            old_lane.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.assertRaises(OSError):
            os.open(old_lane / 'compute.lock', os.O_RDWR | os.O_CREAT, 0o600)
        with self.assertRaises(OSError):
            (old_selection / ('b' * 64)).mkdir(parents=True, exist_ok=True)
        self.assertEqual(learning_state_base(self.home), self.current)
        from context_learning.inference_service import service_root
        self.assertEqual(service_root(create=True, home=self.home),
                         self.current / 'laya-inference')

    def test_service_root_resolved_before_the_seal_finds_the_moved_root(self):
        from context_learning import inference_service, runtime_setup
        state_move.move(self.home, wait=0.2)
        real = runtime_setup.learning_state_base
        calls = []

        def cached_then_real(home=None):
            calls.append(home)
            # The first read was made before the move sealed the old base.
            return self.legacy if len(calls) == 1 else real(home)
        with mock.patch.object(runtime_setup, 'learning_state_base', cached_then_real):
            root = inference_service.service_root(create=False, home=self.home)
        self.assertEqual(root, self.current / 'laya-inference')

    def test_recall_with_an_unreachable_learning_lane_serves_hybrid_rank(self):
        # A recall whose learning state still resolves to the sealed old root:
        # selection and query admission fail, recall serves ranked rows.
        import importlib.util
        from contextlib import ExitStack
        import laya_embed_resident
        import laya_host_response as response
        from context_learning.inference_service import ServiceFailure
        spec = importlib.util.spec_from_file_location(
            'required_selection', SOURCE / 'test/fixtures/laya-required-selection-checks.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        real_admit = laya_embed_resident.admit_query
        with ExitStack() as stack:
            decision, _proof, _rows, _settings = fixture.Checks.host(None, stack)
            stack.enter_context(mock.patch.object(laya_embed_resident, 'admit_query', real_admit))
            stack.enter_context(mock.patch(
                'context_learning.resource_coordination.default_root',
                side_effect=ServiceFailure('unsafe_root')))
            stack.enter_context(mock.patch(
                'context_learning.seam._private_state',
                side_effect=ServiceFailure('unsafe_root')))
            stack.enter_context(mock.patch.object(response, '_request_selector_heal'))
            # The fixture proof has no provider socket; waking it is not under test.
            stack.enter_context(mock.patch.object(response, '_wake_provider',
                                                  return_value='selected_provider_preparing'))
            result = response.serve_selected('recall', 'codex', decision,
                                             {'prompt': 'find card'})
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('wiki/card00.md', context)
        self.assertIn('hybrid rank score', context)

    def test_a_recreated_old_tree_after_a_rename_is_sealed_on_resume(self):
        def die(name):
            if name == 'renamed:laya-inference':
                raise Kill(name)
        with mock.patch.object(state_move, '_step', die):
            with self.assertRaises(Kill):
                state_move.move(self.home, wait=0.2)
        # A process of an earlier bundle recreated the old lane and holds it.
        self.inference.mkdir(mode=0o700)
        (self.inference / 'compute.lock').write_bytes(b'')
        (self.inference / 'compute.lock').chmod(0o600)
        fd = os.open(self.inference / 'compute.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(state_move.LearningMoveDeferred,
                                        'learning_state_stray_busy'):
                state_move.move(self.home, wait=0.2)
            self.assertTrue(self.inference.is_dir())
        finally:
            os.close(fd)
        self.retry_now()
        result = state_move.move(self.home, wait=0.2)
        self.assertEqual(result['status'], 'learning_state_moved', result)
        self.assertEqual(len(result['strays']), 1)
        self.assertTrue(Path(result['strays'][0], 'compute.lock').is_file())
        self.assertTrue(state_move.moved_sentinel(self.inference))
        self.assertEqual(learning_state_base(self.home), self.current)

    def test_selector_heal_requests_are_floored_per_install(self):
        import laya_bundle_upgrade as upgrade
        state = self.home / 'install'
        state.mkdir(mode=0o700)
        spawned = []
        now = time.time_ns()
        for offset in (0, 1, 200):
            upgrade.request('/p', state / 'install-journal.sqlite', 'learning_selector',
                            spawn=spawned.append, now=now + offset * 10**9)
            # A deferred shared status would be due again after 120 seconds.
            upgrade._write_status(state, status='deferred', executingRoot='/x',
                                  attempts=0, reason='x')
        self.assertEqual(spawned, ['/p'])
        upgrade.request('/p', state / 'install-journal.sqlite', 'learning_selector',
                        spawn=spawned.append, now=now + 601 * 10**9)
        self.assertEqual(spawned, ['/p', '/p'])

    def test_a_service_elected_before_the_move_serves_the_moved_root(self):
        old = str(self.inference)
        state_move.move(self.home, wait=0.2)
        self.assertEqual(state_move.current_root(old, home=self.home),
                         str(self.current / 'laya-inference'))

    def test_a_recall_that_cannot_select_asks_the_worker_to_heal(self):
        import laya_host_response as response
        calls = []
        with mock.patch('laya_bundle_upgrade.request',
                        side_effect=lambda *args: calls.append(args)):
            response._request_selector_heal({'projectRoot': '/p', 'journalPath': '/j'},
                                            'adapter_anchor_missing')
            response._request_selector_heal({'projectRoot': '/p', 'journalPath': '/j'},
                                            'timeout')
        self.assertEqual(calls, [('/p', '/j', 'learning_selector')])


MARKET = 'memvane-marketplace'
PLUGIN = 'memvane@' + MARKET
OLD, NEW = '1.0.0', '1.0.1'
DRIVER = r'''import json,sys,time
from pathlib import Path
sys.dont_write_bytecode=True
root=Path(sys.argv[1]);state=Path(sys.argv[2]);mode=sys.argv[3]
sys.path.insert(0,str(root/'core'))
from context_learning import live_select, inference_service, selector_heal
from context_learning.local_cycle import VerifiedLayaTrainer
from context_learning.contracts import digest
from runtime_lease import atomic_json
calls={'attest':0,'prepare':0}
def admission(self,*,cwd):
    assert self.argv[1]==str(root/'core/context_learning/laya_adapter.py'),self.argv
    return {}
def prediction(state,policy,request,artifact_sha,**kwargs):
    case=request['cases'][0]
    return {'schema_version':1,'predictions':[{'request_id':case['request_id'],
        'input_sha256':case['input_sha256'],'selected_ids':['wiki/synthetic.md']}]},{}
VerifiedLayaTrainer.validate_prediction=admission
inference_service.predict=prediction
prepared=[]
def attest(state,policy):
    calls['attest']+=1
    if mode=='defer':
        from resource_deferred import ResourceDeferred
        raise ResourceDeferred('trainer_adaptive_attestation_required')
    import hashlib
    atomic_json(state/'laya-synthetic-attestation.json',{'schema':'qmd-laya-live-proof-v1',
        'verifiedAt':time.time(),'attestation':{'adapter_sha256':hashlib.sha256(
        Path(policy['trainerArgv'][1]).read_bytes()).hexdigest()}})
def prepare(state,policy):
    calls['prepare']+=1;prepared.append(1)
selector_heal.attest=attest;selector_heal.prepare=prepare
selector_heal.admitted=lambda state,argv:bool(prepared) or mode=='ready'
selector_heal.pressure=lambda policy:'learning_prepare_memory_pressure' if mode=='pressure' else None
text='---\ntitle: Synthetic\nstatus: verified\n---\nSynthetic complete body.\n'
def choose():
    return live_select.choose(state,'Synthetic request',[{'id':'wiki/synthetic.md',
        'revision_sha256':digest(text),'source_text':text}],corpus_fingerprint='a'*64)[1]
out={'before':choose()}
try:
    out['heal']=selector_heal.heal(None,state=state)
except Exception as exc:
    out['heal']={'error':str(exc)}
out['after']=choose();out['calls']=calls
out['anchor']=json.loads((state/'live-selector.json').read_text())['trainerArgv'][1]
print(json.dumps(out))
'''


def install_runtime(root, home, model):
    """``stage_generation`` + ``activate_generation`` with synthetic uv and probe."""
    from types import SimpleNamespace
    from context_learning.runtime_installer import activate_generation, stage_generation
    from context_learning.runtime_setup import MANAGED_DEPENDENCIES, probe_runtime
    from context_learning.laya_adapter import model_identity

    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    root.parent.mkdir(parents=True, exist_ok=True)
    for path in (root.parent, root.parent.parent):
        path.chmod(0o700)
    uv = home / 'uv'
    uv.write_text('synthetic uv')
    uv.chmod(0o700)
    lock = home / 'requirements.lock'
    lock.write_text(''.join(f'{name}=={version} --hash=sha256:{"a" * 64}\n'
                            for name, version in MANAGED_DEPENDENCIES.items()))

    def runner(args, **_kwargs):
        if args[1:] == ['--version']:
            return SimpleNamespace(returncode=0, stdout='uv 0.12.20 (synthetic)')
        if args[1:3] == ['python', 'install']:
            generation = Path(args[args.index('--install-dir') + 1]).parent
            python = generation / 'python/cpython-3.12.14-macos-aarch64-none/bin/python3.12'
            python.parent.mkdir(parents=True)
            python.write_text('synthetic python')
            python.chmod(0o700)
        elif args[1] == 'venv':
            executable = Path(args[2]) / 'bin/python'
            executable.parent.mkdir(parents=True)
            executable.write_text('synthetic venv')
            executable.chmod(0o700)
        return SimpleNamespace(returncode=0, stdout='')

    def fake_runtime(_):
        return {'python': '3.12.14', 'versions': dict(MANAGED_DEPENDENCIES),
                'laya_import': '0.3.20', 'torch_import': '2.14.0',
                'mps_available': True, 'laya_direct_url': None}

    def probe(executable, *, managed_root):
        return probe_runtime(executable, managed_root=managed_root, runner=fake_runtime)
    staged = stage_generation(root, uv, sha(uv), lock, sha(lock), runner=runner, probe=probe)
    adapter = home / 'activation-adapter.py'
    adapter.write_text('synthetic adapter')

    def proof(executable, adapter_path, model_dir):
        identity = probe(executable, managed_root=root)['runtime_identity_sha256']
        return {'probe': {'runtime_identity_sha256': identity}, 'attestation': {
            'schema_version': 1, 'runtime_identity_sha256': identity,
            'base_model_sha256': model_identity(model_dir), 'adapter_sha256': sha(adapter_path),
            'data_kind': 'synthetic_compact_wiki', 'checks': {
                'tokenization_no_truncation': True, 'synthetic_inference': True,
                'synthetic_one_step_train': True, 'checkpoint_reload': True,
                'fixed_selection_mapping': True}}}
    activate_generation(root, staged['executable'], adapter, model, prover=proof, probe=probe)
    return staged


class SelectorHealTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lsh-')
        self.addCleanup(temp.cleanup)
        home = self.home = Path(temp.name).resolve() / 'User Home'
        home.mkdir(mode=0o700)
        self.roots = {}
        for host in ('claude', 'codex'):
            for version in (OLD, NEW):
                root = home / ('.' + host) / 'plugins/cache' / MARKET / 'memvane' / version
                root.mkdir(parents=True)
                shutil.copytree(SOURCE / 'core', root / 'core',
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                for directory in ('.claude-plugin', '.codex-plugin'):
                    private(root / directory / 'plugin.json', {'name': 'memvane', 'version': version})
                private(root / 'package.json', {'name': 'memvane', 'version': version})
                self.roots[host, version] = root
        private(home / '.claude/plugins/installed_plugins.json',
                {'version': 2, 'plugins': {PLUGIN: [{'scope': 'user', 'version': NEW,
                 'installPath': str(self.roots['claude', NEW]), 'gitCommitSha': 'a' * 40}]}})
        private(home / '.claude/plugins/known_marketplaces.json',
                {MARKET: {'source': {'source': 'github', 'repo': 'zbdulee/memvane'}}})
        self.env = {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(home / '.codex'),
                    'PYTHONDONTWRITEBYTECODE': '1', 'QMD_RECALL_LOG': ''}
        model = home / 'model'
        for name in ('model.safetensors', 'rl_agent_config.json', 'encoder/config.json',
                     'tokenizer/tokenizer.json', 'tokenizer/tokenizer_config.json'):
            file = model / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'synthetic bytes')
        # The official learning runtime, staged and activated through the
        # installer itself (uv, the interpreter run and the attestation are
        # synthetic); its interpreter is never executed here.
        support = (home / 'Library' / 'Application Support' / 'memvane' if sys.platform == 'darwin'
                   else home / '.local' / 'share' / 'memvane')
        self.runtime = install_runtime(support / 'runtimes' / 'laya', home, model)
        self.interpreter = Path(self.runtime['executable'])
        self.state = home / 'state'
        self.state.mkdir(mode=0o700)
        artifact = self.state / 'head.artifact'
        artifact.write_bytes(b'head')
        artifact.chmod(0o600)
        private(self.state / 'active-checkpoint.json',
                {'schema_version': 1, 'artifact_path': str(artifact),
                 'artifact_sha256': hashlib.sha256(b'head').hexdigest()})
        # The anchor names a superseded cache the host already removed.
        gone = home / '.claude/plugins/cache' / MARKET / 'memvane' / '0.9.0'
        self.policy = {'schema': 'qmd-live-selector-v1',
                       'trainerArgv': [str(self.interpreter),
                                       str(gone / 'core/context_learning/laya_adapter.py'),
                                       str(model)],
                       'timeoutSeconds': 5, 'maxRssMib': 512}
        private(self.state / 'live-selector.json', self.policy)

    def drive(self, mode='heal'):
        out = subprocess.check_output([sys.executable, '-c', DRIVER,
                                       str(self.roots['claude', NEW]), str(self.state), mode],
                                      env=self.env, text=True, timeout=60)
        return json.loads(out)

    def test_a_removed_anchor_is_rebound_and_local_selection_returns(self):
        report = self.drive()
        self.assertEqual(report['before'], 'adapter_anchor_missing', report)
        self.assertEqual(report['heal'], {'status': 'selector_prepared', 'rebound': True})
        self.assertEqual(report['anchor'], str(self.roots['claude', NEW] /
                                               'core/context_learning/laya_adapter.py'))
        self.assertEqual(report['after'], 'selected', report)
        self.assertEqual(report['calls'], {'attest': 1, 'prepare': 1})

    def current_adapter(self):
        return str(self.roots['claude', NEW] / 'core/context_learning/laya_adapter.py')

    def test_a_policy_outside_the_official_runtime_runs_nothing(self):
        private(self.state / 'live-selector.json',
                {**self.policy, 'trainerArgv': [sys.executable] + self.policy['trainerArgv'][1:]})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_runtime_not_official'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_an_interpreter_changed_since_its_prepared_evidence_runs_nothing(self):
        private(self.state / 'laya-inference-manifest.json',
                {os.path.realpath(self.interpreter): [[0], 'e' * 64]})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_runtime_changed'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def profile(self, argv):
        sys.path.insert(0, str(SOURCE / 'core'))
        from context_learning.laya_adapter import model_identity
        from context_learning.learning_profile import digest
        from context_learning.local_cycle import _hash
        policy = {**self.policy, 'trainerArgv': argv}
        private(self.state / 'live-selector.json', policy)
        row = {'trainerArgv': argv, 'livePolicySha256': _hash(self.state / 'live-selector.json'),
               'modelIdentitySha256': model_identity(argv[2])}
        private(self.state / 'learning-profile.json', {'acceptedSha256': digest(row),
                                                       'proposal': row})
        private(self.state / 'laya-inference-manifest.json',
                {os.path.realpath(self.interpreter): [[0], hashlib.sha256(
                    self.interpreter.read_bytes()).hexdigest()]})

    def test_an_intact_profile_naming_this_policy_is_healed(self):
        argv = [str(self.interpreter), self.current_adapter(), self.policy['trainerArgv'][2]]
        self.profile(argv)
        report = self.drive()
        self.assertEqual(report['heal'], {'status': 'selector_prepared', 'rebound': False})
        self.assertEqual(report['calls'], {'attest': 1, 'prepare': 1})

    def test_a_profile_approving_another_interpreter_or_model_runs_nothing(self):
        argv = [str(self.interpreter), self.current_adapter(), self.policy['trainerArgv'][2]]
        self.profile(argv)
        envelope = json.loads((self.state / 'learning-profile.json').read_text())
        row = {**envelope['proposal'], 'trainerArgv': ['/other/python'] + argv[1:]}
        sys.path.insert(0, str(SOURCE / 'core'))
        from context_learning.learning_profile import digest
        private(self.state / 'learning-profile.json', {'acceptedSha256': digest(row),
                                                       'proposal': row})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_profile_rebind_required'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_a_held_prepare_lease_defers_and_runs_nothing(self):
        lock = self.home / '.local' / 'state' / 'memvane' / 'learning-prepare.lock'
        lock.parent.mkdir(parents=True)
        for path in (lock.parent, lock.parent.parent, lock.parent.parent.parent):
            path.chmod(0o700)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = self.drive()
        finally:
            os.close(fd)
        self.assertEqual(report['heal'], {'error': 'learning_prepare_busy'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_memory_pressure_defers_and_runs_nothing(self):
        report = self.drive('pressure')
        self.assertEqual(report['heal'], {'error': 'learning_prepare_memory_pressure'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_a_generation_whose_recorded_identity_changed_runs_nothing(self):
        prepared = self.interpreter.parent.parent.parent / 'prepared.json'
        value = json.loads(prepared.read_text())
        prepared.chmod(0o600)
        private(prepared, {**value, 'runtime_identity_sha256': 'f' * 64})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_runtime_changed'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_a_pointer_without_its_activation_attestation_runs_nothing(self):
        active = self.interpreter.parents[4] / 'active.json'
        pointer = json.loads(active.read_text())
        private(active, {'executable': pointer['executable'],
                         'runtime_identity_sha256': pointer['runtime_identity_sha256']})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_runtime_not_installed'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_a_pointer_to_an_unprepared_interpreter_runs_nothing(self):
        prepared = self.interpreter.parent.parent.parent / 'prepared.json'
        prepared.unlink()
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_runtime_not_installed'})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_ready_evidence_is_left_alone(self):
        report = self.drive('ready')
        self.assertEqual(report['heal'], {'status': 'selector_ready', 'rebound': True})
        self.assertEqual(report['calls'], {'attest': 0, 'prepare': 0})

    def test_an_accepted_learning_profile_is_not_rebound(self):
        private(self.state / 'learning-profile.json', {'proposal': {}})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'learning_profile_rebind_required'})
        self.assertEqual(report['anchor'], self.policy['trainerArgv'][1])
        self.assertEqual(report['after'], 'adapter_anchor_missing')

    def test_a_running_adapter_that_is_not_installed_is_not_adopted(self):
        private(self.home / '.claude/plugins/installed_plugins.json',
                {'version': 2, 'plugins': {}})
        report = self.drive()
        self.assertEqual(report['heal'], {'error': 'adapter_not_official'})
        self.assertEqual(report['anchor'], self.policy['trainerArgv'][1])

    def test_an_attestation_deferral_is_reported_not_bypassed(self):
        report = self.drive('defer')
        self.assertEqual(report['heal'], {'error': 'trainer_adaptive_attestation_required'})
        self.assertEqual(report['calls'], {'attest': 1, 'prepare': 0})


def tearDownModule():
    if _mtimes() != BEFORE:
        raise AssertionError('real learning state changed: ' + json.dumps(_mtimes()))


if __name__ == '__main__':
    unittest.main()
