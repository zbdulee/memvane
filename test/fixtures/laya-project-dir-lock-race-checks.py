#!/usr/bin/env python3
"""Real selected-install moves and kernel flocks; no paid compile or model.

Composition reuses only the existing MoveCase/GitBranchTests setup. Bundle
identity/provider authority remains synthetic as in that fixture. The worker
race runs actual main/recovery/claim/requeue; a synchronous lock-entry barrier
performs the normal move before the real run-lock acquisition, with no sleeps
or background workers. Batch admission is held closed to forbid extraction.
"""
from contextlib import ExitStack, contextmanager, redirect_stdout
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('project_move_fixture_for_lock_races',
                                             HERE / 'laya-project-dir-move-checks.py')
MOVE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MOVE)
import compile_paths as cp
import laya_fresh_wiki_origin as fresh
import laya_install_journal as journal
import laya_ledger_common as ledger
import laya_project_dir_move as move
import laya_product_install as product
import project_dir
import settings_lock
import wiki_compile_enqueue as enqueue
import wiki_compile_worker as worker
import wiki_freshness
import wiki_selected_laya

REAL_FLOCK = fcntl.flock
REAL_OPEN = os.open
REAL_RUN_LOCK = worker.try_run_lock
REAL_QUEUE = worker._safe_queue_path
REAL_RECOVER = worker.recover_pending_refreshes
REAL_REQUEUE = worker.requeue_lines


def inode(info):
    return [info.st_dev, info.st_ino]


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


class ProjectMoveLockRaces(unittest.TestCase):
    def fixture(self, *, adopt=False):
        cls = MOVE.GitBranchTests if adopt else MOVE.MoveCase
        case = cls('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        if adopt:
            case.pull_rename()
        self.assertEqual(project_dir.layout(case.project), 'adopt' if adopt else 'legacy')
        self.assertEqual(move.needed(case.project), 'adopt' if adopt else 'rename')
        return case

    @staticmethod
    def private_lock(path):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)

    @staticmethod
    def protected(case):
        files = {}
        for folder in (case.auto, case.project / '.memvane'):
            if folder.exists():
                for path in folder.rglob('*'):
                    if path.is_file() and not path.name.endswith('.lock'):
                        files[str(path)] = path.read_bytes()
        files[str(case.state / fresh.LEDGER)] = (case.state / fresh.LEDGER).read_bytes()
        files[str(case.state / 'reviewed-route.json')] = (case.state / 'reviewed-route.json').read_bytes()
        return files

    def test_adopt_holds_the_current_operation_inode_at_moved_and_on_resume(self):
        for resume in (False, True):
            with self.subTest(resume=resume), ExitStack() as cleanup:
                case = self.fixture(adopt=True)
                cleanup.callback(case.doCleanups)
                old = case.auto / ledger.OPERATION_LOCK
                current = case.project / '.memvane' / ledger.OPERATION_LOCK
                # The selected fixture can already own an empty old lock.
                for path in (old, current):
                    if not path.exists():
                        self.private_lock(path)
                    path.chmod(0o600)
                initial = {'legacy': inode(old.stat()), 'current': inode(current.stat())}
                self.assertNotEqual(initial['legacy'], initial['current'])
                if resume:
                    def interrupted(phase):
                        if phase == 'fenced':
                            raise MOVE.Kill()
                    with self.assertRaises(MOVE.Kill):
                        case.move(step=interrupted)
                    self.assertTrue(move.intent_present(case.state))
                    self.assertTrue(old.exists() and current.exists())
                held, observations = [], []
                probing = False
                def observed_flock(fd, flags):
                    answer = REAL_FLOCK(fd, flags)
                    if flags & fcntl.LOCK_EX and not probing:
                        info = os.fstat(fd)
                        if inode(info) in initial.values():
                            held.append(fd)
                    return answer
                def checkpoint(phase):
                    nonlocal probing
                    if phase != 'moved':
                        return
                    actual_held = []
                    for fd in held:
                        try:
                            info = os.fstat(fd)
                        except OSError:
                            continue
                        actual_held.append({'fd': fd, 'inode': inode(info),
                                            'nlink': info.st_nlink})
                    fd = os.open(current, os.O_RDWR | os.O_NOFOLLOW)
                    acquired = False
                    probing = True
                    try:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                        except BlockingIOError:
                            pass
                        observations.append({'current': inode(os.fstat(fd)),
                            'held': actual_held, 'contenderAcquired': acquired})
                    finally:
                        if acquired:
                            REAL_FLOCK(fd, fcntl.LOCK_UN)
                        os.close(fd)
                        probing = False
                with mock.patch.object(fcntl, 'flock', observed_flock):
                    result = case.move(step=checkpoint)
                emit({'case': 'adopt-operation', 'resume': resume, 'initial': initial,
                      'moved': observations, 'result': result['status']})
                self.assertEqual(result['status'], 'project_dir_move_complete')
                self.assertEqual(len(observations), 1, 'actual moved checkpoint must execute')
                self.assertFalse(observations[0]['contenderAcquired'],
                                 'mover must hold the current named operation inode')
                self.assertIn(observations[0]['current'],
                              [row['inode'] for row in observations[0]['held']])
                case.assert_moved()
                case.doCleanups()

    def test_a_live_compile_run_refuses_rename_and_adopt_before_the_fence(self):
        for adopt in (False, True):
            with self.subTest(adopt=adopt), ExitStack() as cleanup:
                case = self.fixture(adopt=adopt)
                cleanup.callback(case.doCleanups)
                queue = REAL_QUEUE(case.project, worker.DEFAULT_SOURCE_QUEUE)
                self.assertIsNotNone(queue)
                queue.write_text('{"fixture": "queue must remain"}\n', encoding='utf-8')
                owner = REAL_RUN_LOCK(case.project)
                self.assertIsNotNone(owner, 'fixture must own the actual compile flock')
                before = self.protected(case)
                phases, reason, result = [], '', None
                try:
                    try:
                        result = case.move(step=phases.append)
                    except move.ProjectDirMoveBlocked as exc:
                        reason = str(exc)
                    if isinstance(result, dict):
                        reason = result.get('reason', '')
                    emit({'case': 'compile-held', 'adopt': adopt,
                          'ownerInode': inode(os.fstat(owner)), 'phases': phases,
                          'reason': reason, 'result': result})
                    self.assertRegex(reason, r'(?:compile.*busy|busy.*compile)',
                                     'a held compile run must explicitly defer the move')
                    self.assertNotIn('prepared', phases)
                    self.assertNotIn('fenced', phases)
                    self.assertFalse(move.intent_present(case.state))
                    self.assertIsNone(move.read_pointer(case.project))
                    self.assertEqual(self.protected(case), before)
                    self.assertEqual(project_dir.layout(case.project),
                                     'adopt' if adopt else 'legacy')
                finally:
                    os.close(owner)
                case.doCleanups()

    def test_worker_rebases_queue_after_a_move_before_its_actual_run_lock(self):
        case = self.fixture()
        source = case.project / 'source.md'
        source.write_text('# Fixture source\nA synthetic compile revision.\n', encoding='utf-8')
        current_queue = case.project / '.memvane' / 'compile' / cp.SOURCE_QUEUE
        computed, recovered, requeued, boundaries = [], [], [], []
        in_mover = False
        out = io.StringIO()
        with ExitStack() as stack:
            # Only host bundle/executing-copy authority is synthetic. Actual
            # selected(), journal/route/store proof and source admission run.
            stack.enter_context(mock.patch.object(journal, '_inspect_identity',
                                                   return_value=case.proof))
            stack.enter_context(mock.patch.object(journal, '_verified_executing_root',
                                                   return_value=str(case.plugin)))
            baseline = wiki_selected_laya.selected(case.project)
            self.assertTrue(baseline[1]['productRouteAllowed'])
            cfg = MOVE.config.find_project_config(str(case.project))['config']
            initial_queue = REAL_QUEUE(case.project, worker.DEFAULT_SOURCE_QUEUE)
            self.assertEqual(initial_queue.parent.parent, case.auto)
            pending = wiki_freshness.record_pending_refresh(case.project, 'source.md', 'claude')
            self.assertIsNotNone(pending)
            job = enqueue._source_record(str(source), str(case.project),
                                          str(case.project), cfg, 'claude')
            self.assertIsNotNone(job, 'real source admission must accept the synthetic raw source')
            enqueue._append_jsonl(initial_queue, [job])
            def queue_path(*args, **kwargs):
                path = REAL_QUEUE(*args, **kwargs)
                computed.append(str(path))
                return path
            def acquire(root):
                nonlocal in_mover
                if not boundaries and not in_mover:
                    in_mover = True
                    try:
                        result = case.move()
                        self.assertEqual(result['status'], 'project_dir_move_complete')
                        self.assertFalse(case.auto.exists(), 'normal move itself removes legacy')
                        boundaries.append({'computedBeforeLock': list(computed),
                                           'moveStatus': result['status']})
                    finally:
                        in_mover = False
                fd = REAL_RUN_LOCK(root)
                self.assertIsNotNone(fd, 'worker must acquire a real kernel run lock')
                if not in_mover:
                    boundaries[-1]['runInode'] = inode(os.fstat(fd))
                return fd
            def recovery(root, config, queue):
                recovered.append(str(queue))
                return REAL_RECOVER(root, config, queue)
            def requeue(path, lines):
                requeued.append(str(path))
                return REAL_REQUEUE(path, lines)
            stack.enter_context(mock.patch.dict(os.environ, {'QMD_SANDBOX': '',
                'QMD_SETUP_GUARD_FIXTURE': '1', 'QMD_RECALL_LOG': ''}))
            stack.enter_context(mock.patch.object(worker, '_safe_queue_path', queue_path))
            stack.enter_context(mock.patch.object(worker, 'try_run_lock', acquire))
            stack.enter_context(mock.patch.object(worker, 'recover_pending_refreshes', recovery))
            stack.enter_context(mock.patch.object(worker, 'requeue_lines', requeue))
            stack.enter_context(mock.patch.object(worker, 'batch_ready', return_value=False))
            stack.enter_context(mock.patch.object(worker, '_run_verify_pass', return_value=None))
            stack.enter_context(mock.patch.object(worker, 'run_extractor',
                                                   side_effect=AssertionError('no extractor allowed')))
            stack.enter_context(mock.patch.object(sys, 'argv',
                ['wiki_compile_worker.py', '--cwd', str(case.project), '--json']))
            with redirect_stdout(out):
                code = worker.main()
        emit({'case': 'worker-queue-lock-cutoff', 'code': code,
              'computed': computed, 'recoveryQueue': recovered, 'requeueQueue': requeued,
              'boundaries': boundaries, 'legacyRecreated': case.auto.exists(),
              'workerReport': out.getvalue()})
        self.assertEqual(code, 0)
        self.assertEqual(len(boundaries), 1, 'exactly one real move at lock entry')
        self.assertTrue(recovered, 'actual recovery must execute')
        self.assertTrue(requeued, 'actual claim/batch-defer/requeue must execute')
        self.assertEqual(recovered, [str(current_queue)])
        self.assertTrue(all(path == str(current_queue) for path in requeued))
        self.assertFalse(case.auto.exists(), 'worker must not recreate stale queue parents')
        rows = worker.read_queue(current_queue)
        self.assertTrue(any(job and job.get('source', {}).get('path') == 'source.md'
                            for _raw, job in rows), 'source work remains queued')
        case.assert_moved()

    def test_new_settings_and_card_waiters_refuse_the_unlinked_legacy_inode(self):
        for kind in ('settings', 'card'):
            with self.subTest(kind=kind), ExitStack() as cleanup:
                case = self.fixture()
                # Initialize only a local disposable Git repo; the settings
                # and cards still start in legacy before the waiter opens FD.
                MOVE.git(case.project, 'init', '-q', '-b', 'before')
                MOVE.git(case.project, 'add', '.auto-context/settings.json', '.auto-context/wiki')
                MOVE.git(case.project, 'commit', '-q', '-m', 'before waiter move')
                cleanup.callback(case.doCleanups)
                if kind == 'settings':
                    path = case.auto / settings_lock.LOCK_NAME
                    acquire = lambda: settings_lock.locked(case.project)
                else:
                    (case.auto / 'compile').mkdir(mode=0o700, exist_ok=True)
                    path = case.auto / 'compile' / cp.CARD_WRITE_LOCK
                    acquire = lambda: cp.card_write_lock(case.project)
                opened, release, attempted = threading.Event(), threading.Event(), threading.Event()
                seen, outcome = [], []
                waiter = None
                def observed_open(name, flags, *args, **kwargs):
                    fd = REAL_OPEN(name, flags, *args, **kwargs)
                    if threading.current_thread() is waiter and Path(name) == path:
                        seen.append({'openedInode': inode(os.fstat(fd)), 'fd': fd})
                        opened.set()
                        # This handshake spans a real local Git pull/rename;
                        # it is fixture coordination, not a product wait window.
                        if not release.wait(60):
                            os.close(fd)
                            raise AssertionError('waiter release deadline exceeded')
                    return fd
                def observed_flock(fd, flags):
                    if threading.current_thread() is waiter and flags & fcntl.LOCK_EX:
                        attempted.set()
                    return REAL_FLOCK(fd, flags)
                def run_waiter():
                    try:
                        with acquire():
                            outcome.append({'state': 'entered'})
                    except (OSError, ValueError) as exc:
                        outcome.append({'state': 'refused', 'reason': str(exc)})
                    except BaseException as exc:
                        outcome.append({'state': 'fixture_error', 'reason': repr(exc)})
                def step(phase):
                    if phase == 'fenced':
                        release.set()
                        self.assertTrue(attempted.wait(45), 'writer must attempt actual blocking flock')
                with mock.patch.object(os, 'open', observed_open), \
                        mock.patch.object(fcntl, 'flock', observed_flock):
                    waiter = threading.Thread(target=run_waiter, daemon=True)
                    waiter.start()
                    try:
                        self.assertTrue(opened.wait(45), 'writer must first open the legacy FD')
                        MOVE.GitBranchTests.pull_rename(case)
                        self.assertEqual(project_dir.layout(case.project), 'adopt')
                        result = case.move(step=step)
                    finally:
                        release.set()
                        waiter.join(45)
                    self.assertFalse(waiter.is_alive(), 'owned writer thread must be settled')
                emit({'case': 'post-flock-waiter', 'kind': kind, 'opened': seen,
                      'outcome': outcome, 'moveStatus': result['status']})
                self.assertEqual(result['status'], 'project_dir_move_complete')
                self.assertEqual(len(outcome), 1)
                self.assertEqual(outcome[0]['state'], 'refused')
                self.assertTrue(outcome[0].get('reason'))
                case.assert_moved()

    def test_adoption_after_folder_snapshot_cannot_skip_the_new_current_lock(self):
        case = MOVE.GitBranchTests('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        self.assertEqual(move.needed(case.project), 'rename')
        self.assertFalse((case.project / '.memvane').exists())
        queue = REAL_QUEUE(case.project, worker.DEFAULT_SOURCE_QUEUE)
        self.assertIsNotNone(queue)
        queue.write_text('{"fixture": "snapshot race queue must remain"}\n', encoding='utf-8')
        real_install = product._install_lock
        owners, snapshots, phases = [], [], []
        @contextmanager
        def raced_install(state):
            self.assertEqual(Path(state), case.state)
            # _locks has already snapshotted the legacy-only folder list.
            case.pull_rename()
            self.assertEqual(move.needed(case.project), 'adopt')
            current = case.project / '.memvane' / ledger.OPERATION_LOCK
            self.private_lock(current)
            fd = REAL_OPEN(current, os.O_RDWR | os.O_NOFOLLOW)
            owners.append(fd)
            REAL_FLOCK(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            snapshots.append(self.protected(case))
            with real_install(state):
                yield
        reason, result = '', None
        try:
            with mock.patch.object(product, '_install_lock', raced_install):
                try:
                    result = case.move(step=phases.append)
                except (move.ProjectDirMoveBlocked, ledger.CutoverBlocked) as exc:
                    reason = str(exc)
            emit({'case': 'install-entry-adoption', 'reason': reason, 'result': result,
                  'phases': phases, 'targetHeldInodes': [inode(os.fstat(fd)) for fd in owners]})
            self.assertEqual(len(snapshots), 1, 'the actual install-entry race must execute')
            self.assertRegex(reason, 'busy', 'new current operation ownership must defer the move')
            self.assertNotIn('prepared', phases)
            self.assertNotIn('fenced', phases)
            self.assertEqual(self.protected(case), snapshots[0])
            self.assertFalse(move.intent_present(case.state))
            self.assertIsNone(move.read_pointer(case.project))
        finally:
            for fd in owners:
                os.close(fd)


if __name__ == '__main__':
    unittest.main()
