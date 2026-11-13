#!/usr/bin/env python3
"""T1 actual crash/race boundaries on disposable published v6 installations.

Only existing fixture bundle/provider authority and registered-host observation
are synthetic. Forked writers execute real journal/SQLite/flock/fsync code.
Historical code is extracted from local Git only when this test is executed.
"""
from contextlib import closing, contextmanager
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import select
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from unittest import mock
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('reader_recovery_base', HERE / 'laya-journal-reader-checks.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
J = base.J
CTX = multiprocessing.get_context('fork')
EXIT_AT_CHECKPOINT = 91
EXIT_AT_COMMIT_WINDOW = 92
READER_BRIDGE_FIXTURE_COMMIT = '6437108f4c1165f1141114f94b3e32ccc021cb30'
INITIAL = ('v5-locked', 'v5-sealed', 'v5-versioned', 'v5-committed')
COW = ('v5-image-copied', 'v5-image-committed', 'v5-image-fsynced',
       'v5-image-replaced', 'v5-directory-fsynced')

OLD_CAPACITY_WORKER = r'''
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root));signal.alarm(35)
import laya_vector_capacity as vectors; import memvane.sync.product_sync_worker as worker; import memvane.journal.install_journal as worker_install_journal
import laya_incremental_docs as incremental
from laya_index_runtime import LayaIndexRuntime,capacity_store_paths
from memvane.route.index_host_route import wiki_index_roots
from laya_index_vector import qmd_markdown_title
from laya_index_sync import _owner_lock
from memvane.runtime.resource_coordination import Coordinator
decision,proof=p['decision'],p['proof'];profile=proof['vectorProfile']
provider=SimpleNamespace(manifest={'recipeId':'qmd-regex-token-chunks-candidate-v1'},
    profile={'profile_id':profile['profileId'],'model_id':profile['modelId'],
             'embedding_fingerprint':profile['embeddingFingerprint'],'dimension':profile['dimension']})
epoch=proof['selectionEpoch']
real_cas=worker.cas_mark_new_write
def selected(_decision):
    return dict(proof,selectionEpoch=epoch)
def mark(path,expected):
    global epoch
    result=real_cas(path,expected);epoch=result['epoch'];return result
def pause(runtime,provider,**kwargs):
    with _owner_lock(Path(decision['queuePath'])) as available:
        assert available
    print(json.dumps({'event':'old-capacity-outside-queue-lock','queueAvailable':available,
                      'module':worker.__file__}),flush=True)
    assert sys.stdin.readline()=='RELEASE\n'
    snapshot=runtime.catalog.require_complete()
    row=runtime.index.db.execute('SELECT m.path,m.hash,c.body FROM head h JOIN membership m ON m.gen=h.gen '
        'JOIN content c ON c.hash=m.hash WHERE h.scope=? AND m.eligible=1 ORDER BY m.path LIMIT 1',
        (runtime.scope,)).fetchone()
    assert row is not None
    path,sha,body=row
    kwargs['on_document_complete'](snapshot,path,sha,qmd_markdown_title(body,path),body,profile['profileId'])
    return {'state':'deferred','reason':'fixture_pause'}
controller=Coordinator()
with mock.patch.object(controller,'_admit',return_value={'ready':True}), \
     mock.patch.object(LayaIndexRuntime,'request_scan',return_value={'status':'queued'}), \
     mock.patch.object(worker_install_journal,'verify_selected_route_for_write',side_effect=selected), \
     mock.patch.object(worker,'cas_mark_new_write',side_effect=mark), \
     mock.patch.object(incremental,'_verified',return_value=True), \
     mock.patch.object(worker,'_higher_priority_wake',return_value=False), \
     mock.patch.object(worker,'sync_step',return_value={'state':'pending','reason':'scan_pending'}), \
     mock.patch.object(vectors,'pending_vector_fill_step',side_effect=pause):
    result,_=worker._run_capacity(decision,proof,wiki_index_roots(decision['collections']),
        capacity_store_paths(decision['dbPath']),provider,controller,{},None)
    assert result=={'state':'deferred','reason':'fixture_pause'}
'''


def emit(case, **row):
    print(json.dumps(dict(case=case, **row), sort_keys=True), flush=True)


def physical(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        header = os.pread(fd, 100, 0)
        node = os.fstat(fd)
    finally:
        os.close(fd)
    side = Path(str(path) + '-journal')
    return dict(header_version=int.from_bytes(header[60:64], 'big'),
                inode=[node.st_dev, node.st_ino], size=node.st_size,
                journal_exists=side.exists(), journal_size=side.stat().st_size if side.exists() else 0)


def receive(connection, *, event=None, timeout=25):
    deadline, seen = time.monotonic() + timeout, []
    while time.monotonic() < deadline:
        if not connection.poll(max(0, deadline - time.monotonic())):
            break
        try:
            row = connection.recv()
        except EOFError:
            break
        seen.append(row)
        if event is None or row.get('event') == event:
            return row
    raise AssertionError('owned child handshake timed out: event=%r observed=%r' % (event, seen))


@contextmanager
def child(fn):
    parent, remote = CTX.Pipe()

    def run():
        parent.close()
        signal.alarm(45)
        try:
            value = fn(remote)
            remote.send(dict(event='result', ok=True, value=value))
        except BaseException as exc:
            remote.send(dict(event='result', ok=False, type=type(exc).__name__, reason=str(exc)))
        finally:
            remote.close()

    process = CTX.Process(target=run)
    process.start()
    remote.close()
    try:
        yield process, parent
    finally:
        process.join(1)
        if process.is_alive():
            process.terminate()  # Only this test's owned, unreaped child.
            process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()
        if process.is_alive():
            raise AssertionError('owned child could not be reaped')


@contextmanager
def fixture(converted=False):
    helper = base.JournalReaderChecks('runTest')
    helper.setUp()
    try:
        if converted:
            case, reader, bridge, installer, readiness, selected, _ = helper.converted()
        else:
            case = helper.published_case()
            reader, bridge = helper.reader_api(), helper.bridge_api()
            installer = helper.installer(case, reader, bridge)
            _, _, readiness = helper.ready(case, bridge)
            selected = None
        yield helper, case, reader, bridge, installer, readiness, selected
    finally:
        helper.doCleanups()


class RecoveryChecks(unittest.TestCase):
    def test_selected_identity_read_enforces_query_only(self):
        with fixture() as (_helper,case,_reader,_bridge,_installer,_readiness,_selected):
            with J._identity_connection(case.db) as db:
                self.assertEqual(db.execute('PRAGMA query_only').fetchone(),(1,))
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO schema_meta VALUES('identity-write','forbidden')")
            _path,journal=J._open(case.journal,write=False)
            with closing(journal):
                self.assertEqual(journal.execute('PRAGMA query_only').fetchone(),(1,))

    def test_read_selection_retries_only_store_replacement_and_is_bounded(self):
        with fixture(converted=True) as (_,case,reader,_,_,_,_):
            for reason,attempts in (('reader_store_changed',3),('reader_endpoint_invalid',1)):
                with self.subTest(reason=reason), mock.patch.object(
                        reader,'_selection',side_effect=J.JournalEvidenceError(reason)) as selection:
                    with self.assertRaisesRegex(J.JournalEvidenceError,reason):
                        reader.read_selection(case.journal)
                    self.assertEqual(selection.call_count,attempts)

    def test_read_selection_retries_atomic_replacement_during_read(self):
        with fixture(converted=True) as (helper,case,reader,bridge,installer,_,initial):
            target=helper.alternate(case,reader,installer,'reader-replacement.sqlite')
            original=reader._selection
            changed=[]
            def replace_after_selection(db):
                result=original(db)
                if not changed:
                    changed.append(True)
                    with helper.full_authority(case,reader,bridge):
                        reader.cas_reader(case.journal,expected_epoch=initial.epoch,
                            expected_endpoint=initial.preferred_endpoint,target=target,
                            transition_id='3'*32)
                return result
            with mock.patch.object(reader,'_selection',side_effect=replace_after_selection):
                selected=reader.read_selection(case.journal)
            self.assertEqual(selected.epoch,initial.epoch+1)
            self.assertEqual(selected.preferred_endpoint,target.endpoint_id)

    def test_conversion_waits_for_actual_released_capacity_worker_between_queue_leases(self):
        from memvane.kernel.resource_deferred import ResourceDeferred
        with fixture() as values:
            helper,case,reader,bridge,installer,readiness,_=values
            old=self.old_tree(case)
            with helper.full_authority(case,reader,bridge):
                decision=J.select_route(str(case.project),action='recall')
                proof=J.verify_selected_route(decision)
            payload=case.home/'old-capacity-provider.json'
            payload.write_text(json.dumps({'decision':decision,'proof':proof}),encoding='utf8')
            payload.chmod(0o600)
            process=subprocess.Popen([sys.executable,'-B','-I','-c',OLD_CAPACITY_WORKER,str(old),str(payload)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
            try:
                self.assertTrue(select.select([process.stdout],[],[],20)[0],'old capacity checkpoint timed out')
                line=process.stdout.readline()
                if not line:
                    _out,err=process.communicate(timeout=5)
                    self.fail('old capacity worker failed: '+err)
                ready=json.loads(line)
                self.assertEqual(ready['event'],'old-capacity-outside-queue-lock')
                self.assertTrue(ready['queueAvailable'])
                self.assertEqual(Path(ready['module']).resolve(),old/'core/laya_product_sync_worker.py')
                from laya_index_sync import _owner_lock
                with _owner_lock(Path(decision['queuePath'])) as available:
                    self.assertTrue(available, 'old worker must release queue before conversion')
                before=Path(case.journal).read_bytes()
                with helper.full_authority(case,reader,bridge):
                    with self.assertRaises(ResourceDeferred):
                        reader.convert_reader_v5(case.state,case.project,installer=installer,readiness=readiness)
                    self.assertEqual(Path(case.journal).read_bytes(),before)
                    self.assertEqual(J.journal_version(case.journal),4)
                process.stdin.write('RELEASE\n');process.stdin.flush()
                _out,err=process.communicate(timeout=10)
                self.assertEqual(process.returncode,0,err)
                sidecar=case.state/'laya-incremental-docs.sqlite'
                with closing(sqlite3.connect('file:'+str(sidecar)+'?mode=ro',uri=True)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone()[0],1)
                with helper.full_authority(case,reader,bridge):
                    selected=reader.convert_reader_v5(case.state,case.project,installer=installer,readiness=readiness)
                self.assertEqual(reader.read_selection(case.journal).preferred_endpoint,selected.preferred_endpoint)
                emit('actual-released-capacity-provider',release=READER_BRIDGE_FIXTURE_COMMIT,
                     queueAvailable=ready['queueAvailable'],conversionDeferred=True,
                     oldWorkerFinished=True,conversionAfterExit=True,actualHostExecuted=False)
            finally:
                if process.poll() is None:
                    process.kill();process.communicate(timeout=5)

    def snapshot(self, helper, case):
        return dict(data=helper.data_snapshot(case), metadata=helper.metadata_snapshot(case),
                    head=case.head(), bundle=J.inspect_journal(case.journal))

    def unchanged(self, helper, case, before, *, bundle=True):
        self.assertEqual(helper.data_snapshot(case), before['data'])
        self.assertEqual(helper.metadata_snapshot(case), before['metadata'])
        self.assertEqual(case.head(), before['head'])
        if bundle:
            self.assertEqual(J.inspect_journal(case.journal), before['bundle'])
        self.assertEqual(base.published.fresh._load(case.state / case.ledger_name)['phase'], 'active')

    def initial_crash(self, helper, case, reader, bridge, installer, readiness, step, *, spill=False):
        before = self.snapshot(helper, case)

        def writer(pipe):
            opened = []
            actual_open = J._open

            def open_journal(path, *, write):
                result = actual_open(path, write=write)
                if spill and write:
                    result[1].execute('PRAGMA cache_size=1')
                    result[1].execute('PRAGMA cache_spill=ON')
                    opened.append(result[1])
                return result

            def checkpoint(name):
                if name == step:
                    if spill and opened:
                        # Real reads only: exercise pager eviction with the
                        # converter's own uncommitted pages, not fabricated DB bytes.
                        opened[-1].execute('SELECT * FROM sqlite_master').fetchall()
                        opened[-1].execute('SELECT * FROM read_endpoints').fetchall()
                    pipe.send(dict(event='checkpoint', name=name, physical=physical(case.journal)))
                    os._exit(EXIT_AT_CHECKPOINT)

            with helper.full_authority(case, reader, bridge), \
                    mock.patch.object(J, '_open', side_effect=open_journal), \
                    mock.patch.object(J, '_conversion_step', checkpoint), \
                    mock.patch('subprocess.Popen', side_effect=AssertionError('converter spawned a process')):
                reader.convert_reader_v5(case.state, case.project, installer=installer, readiness=readiness)
            raise AssertionError('requested converter checkpoint was not reached')

        with child(writer) as (process, pipe):
            reached = receive(pipe, event='checkpoint')
            process.join(15)
            self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
        raw = physical(case.journal)
        first = None
        try:
            with closing(sqlite3.connect(Path(case.journal).as_uri() + '?mode=ro', uri=True,
                                         isolation_level=None)) as db:
                db.execute('BEGIN')
                version = J._schema_version_read(db)
                self.assertIn(version, (4, 5), 'first reader must not see a mixed schema')
                row = J._row(db)
                self.assertEqual(row['epoch'], before['bundle']['epoch'])
                if version == 5:
                    self.assertEqual(reader._selection(db)[0].epoch, 1)
                else:
                    self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('read_endpoints','reader_selection','reader_transitions')").fetchone()[0], 0)
                first = dict(version=version, complete=True)
        except sqlite3.OperationalError as exc:
            first = dict(hot_rollback_requires_installer='readonly' in str(exc).lower(),
                         readonly_open_failed=True, error=str(exc))
        emit('initial-crash', checkpoint=step, spill=spill, child=reached,
             before_installer=raw, first_read=first,
             header5_hot_candidate=raw['header_version'] == 5 and raw['journal_exists'])
        # Recovery is explicitly an installer operation. No v5 reader is
        # allowed to open mode=rw to repair a hot image.
        try:
            with helper.full_authority(case, reader, bridge):
                fresh_installer = helper.installer(case, reader, bridge)
                _, _, fresh_ready = helper.ready(case, bridge)
                result = reader.convert_reader_v5(case.state, case.project,
                             installer=fresh_installer, readiness=fresh_ready)
                again = reader.convert_reader_v5(case.state, case.project,
                             installer=fresh_installer, readiness=fresh_ready)
        except J.JournalEvidenceError as exc:
            emit('installer-resume-refused', checkpoint=step, spill=spill, reason=str(exc), physical=physical(case.journal))
            self.fail('normal installer cannot resume actual conversion crash: ' + str(exc))
        self.assertEqual(helper.selection(result), helper.selection(again))
        self.assertEqual(result.epoch, 1)
        self.assertEqual(J.journal_version(case.journal), 5)
        self.unchanged(helper, case, before)
        return raw, first

    def test_initial_conversion_child_exit_at_all_actual_checkpoints_is_resumable(self):
        for step in INITIAL:
            with self.subTest(step=step), fixture() as values:
                self.initial_crash(*values[:6], step)

    def test_low_cache_versioned_crash_records_actual_pager_spill_and_installer_recovery(self):
        with fixture() as values:
            raw, first = self.initial_crash(*values[:6], 'v5-versioned', spill=True)
            emit('pager-spill-hypothesis', physical=raw, first_read=first,
                 status=('header5-and-hot-refusal-observed' if raw['header_version'] == 5 and
                         first.get('hot_rollback_requires_installer') else 'header5-hot-window-not-reproduced'))

    def test_bounded_commit_observer_records_only_actual_header5_hot_window_and_installer_recovery(self):
        with fixture() as (helper, case, reader, bridge, installer, readiness, _):
            before = self.snapshot(helper, case)
            original_image = Path(case.journal).read_bytes()
            deadline = time.monotonic() + 10
            observations, captured = [], None
            limit_reason = 'attempt-limit'
            for attempt in range(1, 25):
                if time.monotonic() >= deadline:
                    limit_reason = 'ten-second-budget'
                    break
                if attempt > 1:
                    # Reset only a completed, closed clean input between trials.
                    # No header is forged, and no captured crash is overwritten.
                    self.assertFalse(Path(str(case.journal) + '-journal').exists())
                    Path(case.journal).write_bytes(original_image)

                def probe(pipe):
                    stop, committing = threading.Event(), threading.Event()
                    threads, descriptors, stats = [], [], dict(polls=0, commit_trace_seen=False)
                    actual_open = J._open

                    def trace(sql):
                        if sql.strip().upper() == 'COMMIT':
                            stats['commit_trace_seen'] = True
                            committing.set()

                    def open_journal(path, *, write):
                        result = actual_open(path, write=write)
                        if write:
                            # Synthetic local scheduling/durability settings;
                            # actual DB, transaction and COMMIT are unchanged.
                            result[1].execute('PRAGMA fullfsync=ON')
                            result[1].execute('PRAGMA cache_size=1')
                            result[1].set_trace_callback(trace)
                        return result

                    def checkpoint(name):
                        if name == 'v5-committed':
                            stop.set()
                        if name != 'v5-versioned':
                            return
                        side = Path(str(case.journal) + '-journal')
                        mainfd = os.open(case.journal, os.O_RDONLY | os.O_NOFOLLOW)
                        descriptors.append(mainfd)
                        try:
                            sidefd = os.open(side, os.O_RDONLY | os.O_NOFOLLOW)
                        except OSError as exc:
                            stats['observer_unavailable'] = type(exc).__name__
                            return
                        descriptors.append(sidefd)

                        def observe_commit():
                            watch_deadline = min(deadline, time.monotonic() + 0.5)
                            while not stop.is_set() and time.monotonic() < watch_deadline:
                                if not committing.wait(min(0.001, max(0, watch_deadline - time.monotonic()))):
                                    continue
                                if stop.is_set():
                                    break
                                stats['polls'] += 1
                                try:
                                    main = os.pread(mainfd, 100, 0)
                                    journal = os.pread(sidefd, 28, 0)
                                    fd_info = os.fstat(sidefd)
                                    named = os.stat(side, follow_symlinks=False)
                                except OSError:
                                    break  # COMMIT may already have unlinked the journal.
                                if (len(main) == 100 and len(journal) == 28
                                        and int.from_bytes(main[60:64], 'big') == 5
                                        and journal[:8] == bytes.fromhex('d9d505f920a163d7')
                                        and int.from_bytes(journal[8:12], 'big') > 0
                                        and fd_info.st_size > 512 and fd_info.st_nlink == 1
                                        and os.path.samestat(fd_info, named) and not stop.is_set()):
                                    pipe.send(dict(event='commit-window', header_version=5,
                                        main_magic=main[:16].hex(), journal_magic=journal[:8].hex(),
                                        journal_records=int.from_bytes(journal[8:12], 'big'),
                                        journal_size=fd_info.st_size, commit_trace_seen=True,
                                        journal_node=[fd_info.st_dev, fd_info.st_ino]))
                                    os._exit(EXIT_AT_COMMIT_WINDOW)
                                stop.wait(min(0.0005, max(0, watch_deadline - time.monotonic())))

                        watcher = threading.Thread(target=observe_commit, name='synthetic-commit-observer')
                        threads.append(watcher)
                        watcher.start()

                    try:
                        with helper.full_authority(case, reader, bridge), \
                                mock.patch.object(J, '_open', side_effect=open_journal), \
                                mock.patch.object(J, '_conversion_step', checkpoint):
                            result = reader.convert_reader_v5(case.state, case.project,
                                                installer=installer, readiness=readiness)
                        return dict(selection=helper.selection(result), observer=stats)
                    finally:
                        stop.set()
                        for thread in threads:
                            thread.join(0.5)
                            if thread.is_alive():
                                os._exit(93)  # Owned probe cannot leak a live watcher.
                        for fd in descriptors:
                            os.close(fd)

                events, exhausted = [], False
                with child(probe) as (process, pipe):
                    while time.monotonic() < deadline:
                        if pipe.poll(min(0.01, max(0, deadline - time.monotonic()))):
                            try:
                                event = pipe.recv()
                            except EOFError:
                                break
                            events.append(event)
                            if event.get('event') == 'result':
                                break
                        elif not process.is_alive():
                            break
                    if process.is_alive():
                        process.join(min(0.1, max(0, deadline - time.monotonic())))
                    exhausted = process.is_alive() and time.monotonic() >= deadline
                observed = dict(attempt=attempt, exitcode=process.exitcode, events=events,
                                physical=physical(case.journal))
                observations.append(observed)
                if exhausted:
                    limit_reason = 'ten-second-budget-owned-child-cleanup'
                    break
                window = next((event for event in events if event.get('event') == 'commit-window'), None)
                if window is not None and process.exitcode == EXIT_AT_COMMIT_WINDOW:
                    # A live writer's journal becomes hot only after death.
                    # Confirm readonly SQLite's actual recovery refusal before
                    # classifying the observer's physical candidate as captured.
                    ro_error = None
                    try:
                        with closing(sqlite3.connect(Path(case.journal).as_uri() + '?mode=ro',
                                                     uri=True, isolation_level=None, timeout=0.1)) as db:
                            db.execute('SELECT count(*) FROM sqlite_master').fetchone()
                    except sqlite3.OperationalError as exc:
                        ro_error = str(exc)
                    observed['readonly_error'] = ro_error
                    if (observed['physical']['header_version'] == 5 and
                            observed['physical']['journal_exists'] and ro_error is not None
                            and 'readonly' in ro_error.lower()):
                        captured = observed
                        limit_reason = 'actual-header5-hot-window-captured'
                    else:
                        limit_reason = 'physical-candidate-not-confirmed-hot-after-exit'
                    break
                result = next((event for event in events if event.get('event') == 'result'), None)
                if process.exitcode != 0 or result is None or not result.get('ok'):
                    limit_reason = 'probe-inconclusive-no-product-defect-classification'
                    break
                self.unchanged(helper, case, before)

            emit('bounded-real-commit-probe', max_attempts=24, budget_seconds=10,
                 synthetic_settings=dict(fullfsync=True, cache_size=1), attempts=observations,
                 status='captured' if captured else 'not-reproduced', limit_reason=limit_reason)
            if captured is None:
                # This observational probe is explicitly inconclusive when the
                # real window is not reached; it proves no recovery guarantee.
                return
            recovery_error = None
            try:
                with helper.full_authority(case, reader, bridge):
                    current_installer = helper.installer(case, reader, bridge)
                    _, _, current_ready = helper.ready(case, bridge)
                    selected = reader.convert_reader_v5(case.state, case.project,
                                      installer=current_installer, readiness=current_ready)
            except ValueError as exc:
                recovery_error = str(exc)
            emit('captured-commit-installer-recovery', error=recovery_error,
                 physical=physical(case.journal), original_scope=before['bundle']['scopeId'])
            self.assertIsNone(recovery_error, 'a confirmed initial header5/hot crash must remain installer-recoverable')
            self.assertEqual(selected.epoch, 1)
            self.assertEqual(J.journal_version(case.journal), 5)
            self.unchanged(helper, case, before)

    def cow_crash(self, helper, case, reader, bridge, installer, readiness, initial, step):
        target = helper.alternate(case, reader, installer, 'crash-target.sqlite')
        before = self.snapshot(helper, case)
        sentinel = case.state / '.unrelated-private.sqlite'
        sentinel.write_bytes(b'not a writer staging image')
        sentinel.chmod(0o600)
        sentinel_node = sentinel.stat().st_ino
        args = dict(expected_epoch=initial.epoch, expected_endpoint=initial.preferred_endpoint,
                    target=target, transition_id='1' * 32)

        def writer(pipe):
            def checkpoint(name):
                if name == step:
                    pipe.send(dict(event='checkpoint', name=name, physical=physical(case.journal)))
                    os._exit(EXIT_AT_CHECKPOINT)
            with helper.full_authority(case, reader, bridge), mock.patch.object(J, '_conversion_step', checkpoint):
                reader.cas_reader(case.journal, **args)

        with child(writer) as (process, pipe):
            reached = receive(pipe, event='checkpoint')
            process.join(15)
            self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
        observed = reader.read_selection(case.journal)
        expected_epoch = 2 if step in ('v5-image-replaced', 'v5-directory-fsynced') else 1
        self.assertEqual(observed.epoch, expected_epoch)
        self.assertEqual(observed.preferred_endpoint, target.endpoint_id if expected_epoch == 2 else initial.preferred_endpoint)
        with helper.full_authority(case, reader, bridge):
            result = reader.cas_reader(case.journal, **args)
            replay = reader.cas_reader(case.journal, **args)
        emit('cow-crash', checkpoint=step, child=reached, first=helper.selection(observed),
             resumed=helper.selection(result), replay=helper.selection(replay))
        self.assertEqual(result.epoch, 2)
        self.assertEqual(helper.selection(replay), helper.selection(result))
        self.assertFalse((case.state / '.reader-next.sqlite').exists())
        self.assertFalse(Path(str(case.state / '.reader-next.sqlite') + '-journal').exists())
        self.assertEqual((sentinel.stat().st_ino, sentinel.read_bytes()),
                         (sentinel_node, b'not a writer staging image'))
        self.unchanged(helper, case, before)

    def test_cow_child_exit_at_all_actual_checkpoints_yields_only_complete_old_or_new(self):
        for step in COW:
            with self.subTest(step=step), fixture(converted=True) as values:
                self.cow_crash(*values, step)

    def race(self, *, same=False, bundle_first=False):
        with fixture(converted=True) as (helper, case, reader, bridge, installer, _, initial):
            target = helper.alternate(case, reader, installer, 'race-target-b.sqlite')
            alternate = target if same else helper.alternate(case, reader, installer, 'race-target-c.sqlite')
            before = self.snapshot(helper, case)
            old_node = physical(case.journal)['inode']
            # Two successive image replacements can reuse the original inode
            # once unlinked. Keep it allocated while checking the final image.
            old_image_fd = os.open(case.journal, os.O_RDONLY)
            self.addCleanup(os.close, old_image_fd)
            lock_path = reader._lock_dir(case.route['installId'], create=False) / 'journal-transition.lock'
            stable_lock = [lock_path.stat().st_dev, lock_path.stat().st_ino]
            first_args = dict(expected_epoch=1, expected_endpoint=initial.preferred_endpoint,
                              target=target, transition_id='1' * 32)
            second_args = dict(first_args, target=alternate, transition_id='1' * 32 if same else '2' * 32)

            def contender(first):
                def run(pipe):
                    import fcntl
                    actual_flock = fcntl.flock

                    def flock(fd, operation):
                        pipe.send(dict(event='flock-attempt', operation=operation))
                        return actual_flock(fd, operation)

                    def checkpoint(name):
                        if first and name == 'v5-image-copied':
                            pipe.send(dict(event='held', name=name))
                            receive(pipe, event='release')

                    def operation():
                        if first and bundle_first:
                            row = J.cas_mark_new_write(case.journal, before['bundle']['epoch'])
                            return dict(kind='bundle', epoch=row['epoch'], newWriteSeen=row['newWriteSeen'])
                        selection = reader.cas_reader(case.journal, **(first_args if first else second_args))
                        return dict(kind='reader', selection=helper.selection(selection))

                    with helper.full_authority(case, reader, bridge), \
                            mock.patch.object(J, '_conversion_step', checkpoint), \
                            mock.patch('fcntl.flock', side_effect=flock):
                        try:
                            return dict(outcome=operation(), busy=False)
                        except ValueError as exc:
                            if str(exc) != 'reader_transition_busy':
                                raise
                            # Only this exact, documented NB-lock deferral may
                            # be retried; no changed request or arbitrary retry.
                            pipe.send(dict(event='busy', reason=str(exc),
                                           selection=helper.selection(reader.read_selection(case.journal))))
                            receive(pipe, event='retry')
                            return dict(outcome=operation(), busy=True)
                return run

            samples = []

            def sample_until_result(pipe):
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    samples.append(helper.selection(reader.read_selection(case.journal)))
                    if pipe.poll(0.01):
                        message = pipe.recv()
                        if message.get('event') == 'result':
                            return message
                self.fail('writer result timed out while fresh readers sampled complete images')

            with child(contender(True)) as (one, p1):
                receive(p1, event='held')
                for _ in range(3):
                    samples.append(helper.selection(reader.read_selection(case.journal)))
                with child(contender(False)) as (two, p2):
                    receive(p2, event='flock-attempt')
                    # Current lock is NB. Observe the actual deferral while
                    # first writer holds it, then retry after winner completes.
                    event = receive(p2)
                    if event.get('event') != 'busy':
                        self.fail('contender must report the held transition lock before release: %r' % event)
                    self.assertEqual(event['reason'], 'reader_transition_busy')
                    self.assertEqual(event['selection'], helper.selection(initial))
                    self.assertEqual(J.inspect_journal(case.journal), before['bundle'])
                    p1.send(dict(event='release'))
                    first = sample_until_result(p1)
                    one.join(15)
                    self.assertEqual(one.exitcode, 0)
                    samples.append(helper.selection(reader.read_selection(case.journal)))
                    p2.send(dict(event='retry'))
                    second = sample_until_result(p2)
                    two.join(15)
                    self.assertEqual(two.exitcode, 0)
                    samples.append(helper.selection(reader.read_selection(case.journal)))
            final, final_bundle = reader.read_selection(case.journal), J.inspect_journal(case.journal)
            emit('cas-race', same=same, bundle_first=bundle_first, first=first, second=second,
                 snapshots=samples, final=helper.selection(final), bundle=final_bundle['epoch'],
                 old_inode=old_node, new_inode=physical(case.journal)['inode'], stable_lock=stable_lock)
            self.assertTrue(first['ok'], first)
            if bundle_first or same:
                self.assertTrue(second['ok'], second)
                self.assertEqual(second['value']['outcome']['selection'][0], 2)
            else:
                self.assertFalse(second['ok'], second)
                self.assertEqual(second['reason'], 'stale_reader_cas')
            self.assertEqual(final.epoch, 2)
            self.assertTrue(all(item[0] in (1, 2) for item in samples))
            self.assertEqual(final.preferred_endpoint, alternate.endpoint_id if bundle_first else target.endpoint_id)
            self.assertNotEqual(old_node, physical(case.journal)['inode'])
            self.assertEqual([lock_path.stat().st_dev, lock_path.stat().st_ino], stable_lock)
            self.unchanged(helper, case, before, bundle=not bundle_first)
            if bundle_first:
                self.assertEqual(final_bundle['epoch'], before['bundle']['epoch'] + 1)
                self.assertTrue(final_bundle['newWriteSeen'])

    def test_distinct_concurrent_cas_busy_retry_has_one_winner_and_one_stale(self):
        self.race()

    def test_identical_concurrent_cas_busy_retry_is_exact_replay_without_second_epoch(self):
        self.race(same=True)

    def test_concurrent_bundle_and_reader_cas_preserve_both_independent_epochs(self):
        self.race(bundle_first=True)

    def test_same_uid_and_install_cannot_bypass_held_transition_lock_by_changing_home(self):
        # Primitive coordination only: no route/proof/queue/provider or CAS.
        # Keep the explicit guarded test-runtime namespace identical in both
        # processes; only the second process's HOME changes.
        from memvane.journal import journal_reader as reader
        temporary = tempfile.TemporaryDirectory(prefix='uid-transition-', dir=os.environ['HOME'])
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        home_a, home_b, runtime = (root / name for name in ('home-a', 'home-b', 'shared-runtime'))
        for path in (home_a, home_b, runtime):
            path.mkdir(mode=0o700)
        install_id = '8' * 32
        with mock.patch.dict(os.environ, dict(HOME=str(home_a), MEMVANE_TEST_RUNTIME_ROOT=str(runtime),
                                              QMD_SETUP_GUARD_FIXTURE='1')):
            with reader._transition(install_id, create=True) as held_fd:
                parent_info = os.fstat(held_fd)
                parent_node = [parent_info.st_dev, parent_info.st_ino]

                def contender(_pipe):
                    os.close(held_fd)  # Close the child's inherited duplicate only.
                    with mock.patch.dict(os.environ, {'HOME': str(home_b)}):
                        result = dict(uid=os.getuid(), home=str(Path.home()),
                                      runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'])
                        try:
                            with reader._transition(install_id, create=True) as acquired_fd:
                                info = os.fstat(acquired_fd)
                                result.update(acquired=True, node=[info.st_dev, info.st_ino])
                        except ValueError as exc:
                            result.update(acquired=False, reason=str(exc))
                        return result

                with child(contender) as (process, pipe):
                    result = receive(pipe, event='result')
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                emit('same-uid-different-home-coordination', install_id=install_id,
                     parent_uid=os.getuid(), parent_home=str(home_a), parent_node=parent_node,
                     shared_test_runtime=str(runtime), child=result)
                self.assertTrue(result['ok'], result)
                observed = result['value']
                self.assertEqual(observed['uid'], os.getuid())
                self.assertEqual(observed['runtime'], str(runtime))
                self.assertEqual(observed['home'], str(home_b))
                self.assertFalse(observed['acquired'],
                                 'same UID/install must contend on one lock or refuse changed HOME')
                self.assertTrue(observed.get('reason'), 'a changed HOME must fail closed before any owner action')

    def test_replaced_staging_inode_is_refused_and_unrelated_private_bytes_survive(self):
        with fixture(converted=True) as (helper, case, reader, bridge, installer, _, initial):
            target = helper.alternate(case, reader, installer, 'stage-target.sqlite')
            args = dict(expected_epoch=1, expected_endpoint=initial.preferred_endpoint,
                        target=target, transition_id='1' * 32)

            def writer(pipe):
                def checkpoint(name):
                    if name == 'v5-image-copied':
                        pipe.send(dict(event='checkpoint'))
                        os._exit(EXIT_AT_CHECKPOINT)
                with helper.full_authority(case, reader, bridge), mock.patch.object(J, '_conversion_step', checkpoint):
                    reader.cas_reader(case.journal, **args)

            with child(writer) as (process, pipe):
                receive(pipe, event='checkpoint')
                process.join(15)
                self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
            image = case.state / '.reader-next.sqlite'
            owned = image.stat().st_ino
            image.rename(case.state / 'owned-stage-preserved.sqlite')
            image.write_bytes(b'unrelated private inode at the staging name')
            image.chmod(0o600)
            replacement = image.stat().st_ino
            with helper.full_authority(case, reader, bridge), self.assertRaisesRegex(ValueError, '^reader_image_changed$'):
                reader.cas_reader(case.journal, **args)
            emit('staging-inode-protection', owned_inode=owned, replacement_inode=replacement,
                 selection=helper.selection(reader.read_selection(case.journal)))
            self.assertNotEqual(owned, replacement)
            self.assertEqual(image.read_bytes(), b'unrelated private inode at the staging name')
            self.assertEqual(image.stat().st_ino, replacement)
            self.assertEqual(helper.selection(reader.read_selection(case.journal)), helper.selection(initial))

    def test_v5_readonly_open_refuses_real_hot_journal_without_rw_repair(self):
        with fixture(converted=True) as (helper, case, reader, _, _, _, _):
            def dirty(pipe):
                db = sqlite3.connect(case.journal, isolation_level=None)
                db.execute('PRAGMA cache_size=1')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE synthetic_hot(x)')
                for _ in range(80):
                    db.execute('INSERT INTO synthetic_hot VALUES(randomblob(4000))')
                pipe.send(dict(event='checkpoint', physical=physical(case.journal)))
                os._exit(EXIT_AT_CHECKPOINT)

            with child(dirty) as (process, pipe):
                reached = receive(pipe, event='checkpoint')
                process.join(15)
                self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
            self.assertTrue(Path(str(case.journal) + '-journal').exists())
            before, side = Path(case.journal).read_bytes(), Path(str(case.journal) + '-journal').read_bytes()
            opened, actual_connect = [], sqlite3.connect

            def connect(name, *args, **kwargs):
                opened.append(str(name))
                return actual_connect(name, *args, **kwargs)

            with mock.patch('sqlite3.connect', side_effect=connect), \
                    mock.patch.object(J, '_recover_interrupted_transaction', wraps=J._recover_interrupted_transaction) as repair:
                with self.assertRaises(ValueError) as refused:
                    reader.read_selection(case.journal)
            emit('v5-hot-ro', child=reached, opened=opened, repair_calls=repair.call_count,
                 reason=str(refused.exception), physical=physical(case.journal))
            self.assertTrue(opened)
            self.assertTrue(all('mode=ro' in name for name in opened))
            self.assertEqual(repair.call_count, 0)
            self.assertEqual(Path(case.journal).read_bytes(), before)
            self.assertEqual(Path(str(case.journal) + '-journal').read_bytes(), side)

    def test_issued_endpoint_refuses_actual_bundle_epoch_rollback_without_relabeling_proof(self):
        with fixture(converted=True) as (helper, case, reader, bridge, installer, _, initial):
            target = helper.alternate(case, reader, installer, 'rollback-target.sqlite')
            original_creation = target.record['creation_epoch']
            original_id = target.endpoint_id
            self.assertGreater(original_creation, 1)
            before = self.snapshot(helper, case)
            self.assertEqual(before['bundle']['epoch'], original_creation)
            lowered = original_creation - 1
            # Explicit synthetic metadata-tamper boundary: change only the
            # actual bundle epoch. No substituted proof or capability fields.
            with closing(sqlite3.connect(case.journal)) as db, db:
                db.execute('UPDATE selection SET epoch=? WHERE id=1', (lowered,))
            tampered = J.inspect_journal(case.journal)
            self.assertEqual(dict(tampered, epoch=before['bundle']['epoch']), before['bundle'])
            with helper.full_authority(case, reader, bridge):
                full = J.verify_selected_route(case.decision())
            self.assertTrue(full['productRouteAllowed'])
            self.assertEqual(full['selectionEpoch'], lowered)
            image_before = Path(case.journal).read_bytes()
            error = None
            with helper.full_authority(case, reader, bridge):
                try:
                    reader.cas_reader(case.journal, expected_epoch=initial.epoch,
                        expected_endpoint=initial.preferred_endpoint, target=target, transition_id='6' * 32)
                except ValueError as exc:
                    error = str(exc)
            observed = reader.read_selection(case.journal)
            emit('issued-epoch-rollback-negative', issued_creation_epoch=original_creation,
                 actual_bundle_epoch=tampered['epoch'], actual_fullproof_epoch=full['selectionEpoch'],
                 fullproof_allowed=full['productRouteAllowed'], reason=error,
                 reader=helper.selection(observed), only_bundle_epoch_tampered=True)
            self.assertIsNotNone(error, 'an actual bundle epoch below issuance history must refuse')
            self.assertEqual(helper.selection(observed), helper.selection(initial))
            self.assertEqual(Path(case.journal).read_bytes(), image_before)
            self.assertEqual(J.inspect_journal(case.journal), tampered)
            self.assertEqual((target.record['creation_epoch'], target.endpoint_id), (original_creation, original_id))
            self.unchanged(helper, case, before, bundle=False)

    def test_forward_bundle_epoch_does_not_authorize_same_bytes_replacement_of_issued_db_inode(self):
        with fixture(converted=True) as (helper, case, reader, bridge, installer, _, initial):
            staged = case.state / 'issued-inode-target.sqlite'
            replacement = case.state / 'replacement-inode.sqlite'
            # First merge any committed source WAL into a closed, standalone
            # SQLite backup. Copy those closed snapshot bytes into a second
            # inode before issuance; copying a live DB file would be unsafe.
            with closing(sqlite3.connect(Path(case.db).as_uri() + '?mode=ro', uri=True)) as source, \
                    closing(sqlite3.connect(staged)) as snapshot:
                source.backup(snapshot)
            staged.chmod(0o600)
            snapshot_bytes = staged.read_bytes()
            replacement.write_bytes(snapshot_bytes)
            replacement.chmod(0o600)
            self.assertEqual(staged.read_bytes(), replacement.read_bytes())
            old_node = [staged.stat().st_dev, staged.stat().st_ino]
            new_node = [replacement.stat().st_dev, replacement.stat().st_ino]
            self.assertNotEqual(old_node, new_node)
            before = self.snapshot(helper, case)
            validated = reader._published(case.state, staged, case.decision())
            self.assertEqual(reader._published(case.state, replacement, case.decision()), validated)
            with helper.full_authority(case, reader, bridge):
                target = reader.seal_reader_endpoint(installer, db_path=staged,
                            catalog_path=case.catalog, binding_path=case.store)
            original_creation, original_id = target.record['creation_epoch'], target.endpoint_id
            self.assertEqual(list(target.record['db_node']), old_node)
            marked = J.cas_mark_new_write(case.journal, before['bundle']['epoch'])
            self.assertEqual(marked['epoch'], original_creation + 1)
            os.replace(replacement, staged)
            self.assertEqual(staged.read_bytes(), snapshot_bytes)
            self.assertEqual([staged.stat().st_dev, staged.stat().st_ino], new_node)
            self.assertEqual(reader._published(case.state, staged, case.decision()), validated)
            with helper.full_authority(case, reader, bridge):
                full = J.verify_selected_route(case.decision())
            self.assertTrue(full['productRouteAllowed'])
            self.assertEqual(full['selectionEpoch'], marked['epoch'])
            image_before, error = Path(case.journal).read_bytes(), None
            with helper.full_authority(case, reader, bridge):
                try:
                    reader.cas_reader(case.journal, expected_epoch=initial.epoch,
                        expected_endpoint=initial.preferred_endpoint, target=target, transition_id='7' * 32)
                except ValueError as exc:
                    error = str(exc)
            observed = reader.read_selection(case.journal)
            emit('forward-epoch-replaced-inode-negative', issued_creation_epoch=original_creation,
                 actual_bundle_epoch=marked['epoch'], actual_fullproof_epoch=full['selectionEpoch'],
                 old_node=old_node, replacement_node=new_node, identical_snapshot_bytes=True,
                 actual_published_validation_equal=True, reason=error, reader=helper.selection(observed))
            self.assertIsNotNone(error, 'forward bundle epoch must not permit a replaced issued DB inode')
            self.assertEqual(helper.selection(observed), helper.selection(initial))
            self.assertEqual(Path(case.journal).read_bytes(), image_before)
            self.assertEqual(J.inspect_journal(case.journal), marked)
            self.assertEqual((target.record['creation_epoch'], target.endpoint_id), (original_creation, original_id))
            self.assertEqual(list(target.record['db_node']), old_node)
            self.unchanged(helper, case, before, bundle=False)

    def old_tree(self, case):
        """Extract actual released tracked code at test runtime; never fetch."""
        listed = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '-r', '--name-only', READER_BRIDGE_FIXTURE_COMMIT],
                                capture_output=True, encoding='utf8', timeout=30)
        self.assertEqual(listed.returncode, 0, 'local historical closure unavailable: ' + listed.stderr)
        names = listed.stdout.splitlines()
        wanted = [prefix for prefix in ('core', 'hooks', '.claude-plugin', '.codex-plugin')
                  if any(name.startswith(prefix + '/') for name in names)]
        wanted += [name for name in ('plugin.yaml', '__init__.py') if name in names]
        archived = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', READER_BRIDGE_FIXTURE_COMMIT, '--', *wanted],
                                  capture_output=True, timeout=30)
        self.assertEqual(archived.returncode, 0,
                         'historical archive refused; no bypass: ' + archived.stderr.decode('utf8', 'replace'))
        self.assertLessEqual(len(archived.stdout), 64 * 1024 * 1024)
        root = case.home / 'released-6437108'
        root.mkdir(mode=0o700)
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
            members = archive.getmembers()
            self.assertLessEqual(len(members), 4096)
            for member in members:
                relative = PurePosixPath(member.name)
                self.assertFalse(relative.is_absolute())
                self.assertNotIn('..', relative.parts)
                self.assertTrue(member.isdir() or member.isfile(), 'historical links are not extracted')
                target = root.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with archive.extractfile(member) as source:
                        target.write_bytes(source.read())
                    target.chmod(0o600)
        self.assertTrue((root / 'core/laya_install_journal.py').is_file())
        return root

    def pending_initial(self, values, *, hot=False):
        helper, case, reader, bridge, installer, readiness, _ = values

        def writer(pipe):
            def checkpoint(name):
                if name == 'initial-intent-durable':
                    pipe.send(dict(event='checkpoint', name=name))
                    os._exit(EXIT_AT_CHECKPOINT)
            with helper.full_authority(case, reader, bridge), mock.patch.object(J, '_conversion_step', checkpoint):
                reader.convert_reader_v5(case.state, case.project, installer=installer, readiness=readiness)

        with child(writer) as (process, pipe):
            receive(pipe, event='checkpoint')
            process.join(10)
            self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
        self.assertTrue((case.state / J._INITIAL_INTENT / 'intent.json').is_file())
        if hot:
            self.actual_hot(case.journal)

    def actual_hot(self, path):
        def writer(pipe):
            db = sqlite3.connect(path, isolation_level=None)
            db.execute('PRAGMA cache_size=1')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE synthetic_recovery_hot(x)')
            for _ in range(80):
                db.execute('INSERT INTO synthetic_recovery_hot VALUES(randomblob(4000))')
            pipe.send(dict(event='checkpoint', physical=physical(path)))
            os._exit(EXIT_AT_CHECKPOINT)

        with child(writer) as (process, pipe):
            reached = receive(pipe, event='checkpoint')
            process.join(10)
            self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
        emit('negative-fixture-real-sqlite-hot', observed=reached,
             provenance='synthetic direct transaction; not claimed as converter COMMIT capture')
        self.assertTrue(Path(str(path) + '-journal').exists())

    @contextmanager
    def live_rw_opens(self, path):
        calls, actual = [], sqlite3.connect

        def connect(name, *args, **kwargs):
            value = str(name)
            if ((value == str(path)) or
                    (value.split('?', 1)[0] == Path(path).as_uri() and 'mode=rw' in value)):
                calls.append(value)
            return actual(name, *args, **kwargs)

        with mock.patch('sqlite3.connect', side_effect=connect):
            yield calls

    def test_initial_recovery_restarts_without_issued_registry_and_survives_its_own_crashes(self):
        import laya_journal_recovery as recovery
        for step in ('initial-recovery-proved', 'initial-recovery-rolled-back'):
            with self.subTest(step=step), fixture() as values:
                helper, case, reader, bridge, _, _, _ = values
                before = self.snapshot(helper, case)
                self.pending_initial(values, hot=True)
                reader._issued.clear()
                bridge._ISSUED.clear()

                def resume(pipe):
                    def checkpoint(name):
                        if name == step:
                            pipe.send(dict(event='checkpoint', name=name))
                            os._exit(EXIT_AT_CHECKPOINT)
                    with helper.full_authority(case, reader, bridge), mock.patch.object(J, '_conversion_step', checkpoint):
                        helper.installer(case, reader, bridge)

                with child(resume) as (process, pipe):
                    reached = receive(pipe, event='checkpoint')
                    process.join(10)
                    self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
                self.assertIsNone(J._initial_preimage.get())
                with helper.full_authority(case, reader, bridge):
                    fresh = helper.installer(case, reader, bridge)
                    _, _, ready = helper.ready(case, bridge)
                    selected = reader.convert_reader_v5(case.state, case.project, installer=fresh, readiness=ready)
                    replay = reader.convert_reader_v5(case.state, case.project, installer=fresh, readiness=ready)
                emit('fresh-registry-recovery', checkpoint=reached, reader=helper.selection(selected))
                self.assertEqual(selected.epoch, 1)
                self.assertEqual(helper.selection(selected), helper.selection(replay))
                self.assertFalse((case.state / J._INITIAL_INTENT).exists())
                self.unchanged(helper, case, before)
                self.assertTrue(case.recall(case.decision())[1])

    def test_invalid_initial_preimage_inode_marker_attestation_and_store_refuse_before_live_rw(self):
        for mutation in ('preimage', 'canonical-inode', 'marker', 'attestation', 'store'):
            with self.subTest(mutation=mutation), fixture() as values:
                helper, case, reader, bridge, _, _, _ = values
                self.pending_initial(values)
                if mutation == 'preimage':
                    image = case.state / J._INITIAL_INTENT / 'base.sqlite'
                    image.write_bytes(image.read_bytes() + b'synthetic tamper')
                elif mutation == 'canonical-inode':
                    new = case.state / 'replacement-journal.sqlite'
                    new.write_bytes(Path(case.journal).read_bytes())
                    new.chmod(0o600)
                    previous = Path(case.journal).stat().st_ino
                    os.replace(new, case.journal)
                    self.assertNotEqual(Path(case.journal).stat().st_ino, previous)
                elif mutation == 'marker':
                    marker = case.auto / base.published.fresh.MARKER
                    row = json.loads(marker.read_text(encoding='utf8'))
                    row['projectRoot'] = str(case.home / 'foreign-project')
                    marker.write_text(json.dumps(row), encoding='utf8')
                elif mutation == 'attestation':
                    att = case.state / J.PRODUCTION_ATTESTATION
                    att.write_bytes(att.read_bytes() + b'\n')
                else:
                    with closing(sqlite3.connect(case.db)) as db, db:
                        db.execute('PRAGMA user_version=999')
                original = Path(case.journal).read_bytes()
                data, metadata = helper.data_snapshot(case), helper.metadata_snapshot(case)
                error = None
                with self.live_rw_opens(case.journal) as writes, helper.full_authority(case, reader, bridge):
                    try:
                        helper.installer(case, reader, bridge)
                    except ValueError as exc:
                        error = str(exc)
                emit('initial-evidence-negative', mutation=mutation, reason=error, live_rw_opens=writes)
                self.assertIsNotNone(error)
                self.assertEqual(writes, [])
                self.assertEqual(Path(case.journal).read_bytes(), original)
                self.assertEqual(helper.data_snapshot(case), data)
                self.assertEqual(helper.metadata_snapshot(case), metadata)

    def test_stable_v5_hot_without_initial_intent_refuses_read_and_installer_with_zero_live_rw(self):
        with fixture(converted=True) as (helper, case, reader, bridge, _, _, _):
            self.assertFalse((case.state / J._INITIAL_INTENT).exists())
            self.actual_hot(case.journal)
            original, rollback = Path(case.journal).read_bytes(), Path(str(case.journal) + '-journal').read_bytes()
            errors = []
            with self.live_rw_opens(case.journal) as writes, helper.full_authority(case, reader, bridge):
                for action in (lambda: J.inspect_journal(case.journal),
                               lambda: helper.installer(case, reader, bridge)):
                    try:
                        action()
                    except ValueError as exc:
                        errors.append(str(exc))
            emit('stable-hot-no-intent', reasons=errors, live_rw_opens=writes)
            self.assertEqual(len(errors), 2)
            self.assertEqual(writes, [])
            self.assertEqual(Path(case.journal).read_bytes(), original)
            self.assertEqual(Path(str(case.journal) + '-journal').read_bytes(), rollback)
            actual_pread=os.pread
            inode=Path(case.journal).stat().st_ino
            def reject_header_read(fd,size,offset):
                if os.fstat(fd).st_ino==inode and (size,offset)==(4,60):
                    raise AssertionError('journal version must be checked by SQLite')
                return actual_pread(fd,size,offset)
            with mock.patch.object(os,'pread',side_effect=reject_header_read), self.live_rw_opens(case.journal) as writes:
                with self.assertRaises(J.JournalEvidenceError):
                    J._open(case.journal,write=True)
            self.assertEqual(writes,[])

    def test_preimage_context_denies_write_and_other_journal_and_resets_after_exception(self):
        import laya_journal_recovery as recovery
        with fixture() as values:
            _, case, _, _, _, _, _ = values
            self.pending_initial(values)
            image = recovery._Preimage(case.state, case.project)
            other_state = case.state / 'other-journal'
            other_state.mkdir(mode=0o700)
            other = other_state / 'install-journal.sqlite'
            other.write_bytes(Path(case.journal).read_bytes())
            other.chmod(0o600)
            original = Path(case.journal).read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'scoped synthetic proof failure'):
                with recovery._preimage_reads(image):
                    for path, write in ((case.journal, True), (other, False), (other, True)):
                        with self.assertRaisesRegex(ValueError, '^initial_recovery_read_only$'):
                            J._open(path, write=write)
                    raise RuntimeError('scoped synthetic proof failure')
            self.assertIsNone(J._initial_preimage.get())
            with self.live_rw_opens(case.journal) as writes:
                _, db = J._open(case.journal, write=True)
                db.close()  # No transaction or DML: prove routing, not write permission.
            emit('preimage-context-finally', context_empty=J._initial_preimage.get() is None,
                 normal_live_rw_opens=writes)
            self.assertEqual(len(writes), 1)
            self.assertEqual(Path(case.journal).read_bytes(), original)

    def test_readiness_registry_ignores_home_and_invalid_override_never_uses_live_fallback(self):
        import laya_bridge_readiness as bridge
        from memvane.journal import journal_paths as paths
        temporary = tempfile.TemporaryDirectory(prefix='registry-negative-', dir=os.environ['HOME'])
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        runtime, a, b = (root / name for name in ('runtime', 'home-a', 'home-b'))
        for path in (runtime, a, b):
            path.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': str(runtime), 'HOME': str(a)}):
            first = bridge._registry(create=True)
            with mock.patch.dict(os.environ, {'HOME': str(b)}):
                second = bridge._registry(create=True)
            self.assertEqual(first, second)
            with mock.patch.object(paths.pwd, 'getpwuid', side_effect=AssertionError('live UID fallback forbidden')) as live:
                for override in ('', '/memvane-outside-platform-temp-fixture'):
                    with self.subTest(override=override), mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': override}):
                        with self.assertRaises(ValueError):
                            bridge._registry(create=True)
                self.assertEqual(live.call_count, 0)
        emit('registry-negative', first=str(first), second=str(second), live_fallback_calls=0)

    def test_actual_readonly_recovery_works_without_sqlite_readonly_constant(self):
        with fixture() as values:
            helper, case, reader, bridge, _, _, _ = values
            before = self.snapshot(helper, case)
            self.pending_initial(values, hot=True)
            with closing(sqlite3.connect(Path(case.journal).as_uri() + '?mode=ro', uri=True)) as db:
                with self.assertRaises(sqlite3.OperationalError) as actual:
                    db.execute('PRAGMA application_id').fetchone()
            self.assertIn('readonly', str(actual.exception).lower())
            saved = getattr(sqlite3, 'SQLITE_READONLY', None)
            try:
                if hasattr(sqlite3, 'SQLITE_READONLY'):
                    delattr(sqlite3, 'SQLITE_READONLY')
                self.assertFalse(hasattr(sqlite3, 'SQLITE_READONLY'))
                with helper.full_authority(case, reader, bridge):
                    fresh = helper.installer(case, reader, bridge)
                    _, _, ready = helper.ready(case, bridge)
                    selected = reader.convert_reader_v5(case.state, case.project, installer=fresh, readiness=ready)
            finally:
                if saved is not None:
                    setattr(sqlite3, 'SQLITE_READONLY', saved)
            emit('readonly-constant-absent-control', actual_sqlite_error=str(actual.exception),
                 reader_epoch=selected.epoch, actual_python39_full_suite_claim=False)
            self.assertEqual(selected.epoch, 1)
            self.unchanged(helper, case, before)

    def test_clean_v4_after_durable_intent_preserves_successful_new_write_when_reissued(self):
        with fixture() as values:
            helper, case, reader, bridge, _, _, _ = values
            before = self.snapshot(helper, case)
            self.pending_initial(values)
            self.assertEqual(J.journal_version(case.journal), 4)
            self.assertFalse(Path(str(case.journal) + '-journal').exists())
            marked = J.cas_mark_new_write(case.journal, before['bundle']['epoch'])
            self.assertEqual(marked['epoch'], before['bundle']['epoch'] + 1)
            self.assertTrue(marked['newWriteSeen'])
            before['bundle'] = marked
            reader._issued.clear()
            bridge._ISSUED.clear()
            error, selected = None, None
            try:
                with helper.full_authority(case, reader, bridge):
                    fresh = helper.installer(case, reader, bridge)
                    _, _, ready = helper.ready(case, bridge)
                    selected = reader.convert_reader_v5(case.state, case.project, installer=fresh, readiness=ready)
            except ValueError as exc:
                error = str(exc)
            observed = J.inspect_journal(case.journal)
            emit('clean-v4-forward-write-after-intent', marked_epoch=marked['epoch'],
                 actual_epoch=observed['epoch'], new_write_seen=observed['newWriteSeen'],
                 recovery_error=error, reader=None if selected is None else helper.selection(selected))
            self.assertEqual(observed, marked, 'installer must never undo the accepted write or its epoch')
            self.unchanged(helper, case, before)
            self.assertIsNone(error, 'clean v4 with accepted forward progress must allow fresh conversion')
            self.assertEqual(selected.epoch, 1)
            self.assertEqual(J.journal_version(case.journal), 5)

    def test_repeated_exit_after_real_rehearsal_writes_has_bounded_owned_staging(self):
        import laya_journal_recovery as recovery
        with fixture() as values:
            helper, case, reader, bridge, _, _, _ = values
            before = self.snapshot(helper, case)
            self.pending_initial(values, hot=True)
            canonical = Path(case.journal).read_bytes()
            rollback_path = Path(str(case.journal) + '-journal')
            rollback = rollback_path.read_bytes()
            unrelated = case.state / '.reader-rehearse-unrelated-private'
            unrelated.mkdir(mode=0o700)
            sentinel = unrelated / 'unrelated-owner.txt'
            sentinel.write_bytes(b'unrelated private directory must survive')
            sentinel.chmod(0o600)
            unrelated_inode = unrelated.stat().st_ino
            stages, counts, deaths = set(), [], []
            for attempt in range(2):
                def resume(pipe):
                    reader._issued.clear()
                    bridge._ISSUED.clear()
                    actual_write, completed = recovery._write, set()

                    def written(path, data):
                        result = actual_write(path, data)
                        path = Path(path)
                        completed.add(path)
                        # Observe the completed private DB+journal pair. The
                        # stage directory may be unique or fixed; no production
                        # name/hook is required and no write is fabricated.
                        if (path.name.endswith('.sqlite-journal') and case.state in path.parents
                                and path.parent != case.state):
                            trial = path.with_name(path.name[:-len('-journal')])
                            if trial in completed and trial.is_file() and path.is_file():
                                pipe.send(dict(event='checkpoint', attempt=attempt,
                                    stage=str(path.parent), files=[str(trial), str(path)],
                                    sizes=[trial.stat().st_size, path.stat().st_size]))
                                os._exit(EXIT_AT_CHECKPOINT)
                        return result

                    with helper.full_authority(case, reader, bridge), \
                            mock.patch.object(recovery, '_write', side_effect=written):
                        helper.installer(case, reader, bridge)

                with child(resume) as (process, pipe):
                    reached = receive(pipe, event='checkpoint')
                    process.join(10)
                    self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
                deaths.append(reached)
                stages.add(Path(reached['stage']))
                counts.append(sum(path.exists() for path in stages))
                self.assertEqual(Path(case.journal).read_bytes(), canonical)
                self.assertEqual(rollback_path.read_bytes(), rollback)
                self.assertEqual(unrelated.stat().st_ino, unrelated_inode)
                self.assertEqual(sentinel.read_bytes(), b'unrelated private directory must survive')
            reader._issued.clear()
            bridge._ISSUED.clear()
            with helper.full_authority(case, reader, bridge):
                fresh = helper.installer(case, reader, bridge)
                _, _, ready = helper.ready(case, bridge)
                selected = reader.convert_reader_v5(case.state, case.project, installer=fresh, readiness=ready)
            final_count = sum(path.exists() for path in stages)
            emit('rehearsal-repeat-death', deaths=deaths, owned_remaining_after_each=counts,
                 owned_remaining_after_recovery=final_count, reader=helper.selection(selected),
                 unrelated_preserved=sentinel.read_bytes() == b'unrelated private directory must survive')
            self.assertEqual(selected.epoch, 1)
            self.unchanged(helper, case, before)
            self.assertEqual(unrelated.stat().st_ino, unrelated_inode)
            self.assertEqual(sentinel.read_bytes(), b'unrelated private directory must survive')
            self.assertLessEqual(max(counts + [final_count]), 1,
                                 'the same recovery cannot accumulate unbounded owned rehearsal copies')

    def test_empty_rehearsal_stage_after_owner_creation_or_cleanup_crash_is_resumable(self):
        import laya_journal_recovery as recovery
        for phase in ('before-owner-write', 'after-owner-unlink', 'after-prefix-write'):
            with self.subTest(phase=phase), fixture() as values:
                helper, case, reader, bridge, _, _, _ = values
                before = self.snapshot(helper, case)
                self.pending_initial(values, hot=True)
                canonical = Path(case.journal).read_bytes()
                side = Path(str(case.journal) + '-journal')
                rollback = side.read_bytes()

                def resume(pipe):
                    reader._issued.clear()
                    bridge._ISSUED.clear()
                    actual_write, actual_unlink = recovery._write, Path.unlink

                    def checkpoint(path):
                        pipe.send(dict(event='checkpoint', phase=phase,
                            stage=str(path.parent), node=[path.parent.stat().st_dev,
                                                        path.parent.stat().st_ino],
                            entries=sorted(p.name for p in path.parent.iterdir()),
                            provenance=('synthetic partial metadata write' if phase == 'after-prefix-write'
                                        else 'actual filesystem checkpoint')))
                        os._exit(EXIT_AT_CHECKPOINT)

                    def written(path, data):
                        path = Path(path)
                        if phase == 'before-owner-write' and path.name == 'owner.json':
                            checkpoint(path)  # The actual private mkdir has completed.
                        if phase == 'after-prefix-write' and path.name == 'owner.json':
                            # A synthetic torn-metadata control, not a claim
                            # that a natural write was observed to tear.
                            with os.fdopen(recovery._exclusive(path), 'wb') as out:
                                out.write(data[:len(data) // 2])
                                out.flush()
                                os.fsync(out.fileno())
                            checkpoint(path)
                        return actual_write(path, data)

                    def unlinked(path, *args, **kwargs):
                        result = actual_unlink(path, *args, **kwargs)
                        if (phase == 'after-owner-unlink' and path.name == 'owner.json'
                                and path.parent.name.startswith('.reader-rehearse-')):
                            checkpoint(path)  # Observe the actual completed unlink.
                        return result

                    with helper.full_authority(case, reader, bridge), \
                            mock.patch.object(recovery, '_write', side_effect=written), \
                            mock.patch.object(Path, 'unlink', unlinked):
                        helper.installer(case, reader, bridge)

                with child(resume) as (process, pipe):
                    reached = receive(pipe, event='checkpoint')
                    process.join(10)
                    self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
                self.assertEqual(reached['entries'], ['owner.json'] if phase == 'after-prefix-write' else [])
                self.assertEqual(Path(case.journal).read_bytes(), canonical)
                self.assertEqual(side.read_bytes(), rollback)
                reader._issued.clear()
                bridge._ISSUED.clear()
                error, selected = None, None
                try:
                    with helper.full_authority(case, reader, bridge):
                        fresh = helper.installer(case, reader, bridge)
                        _, _, ready = helper.ready(case, bridge)
                        selected = reader.convert_reader_v5(case.state, case.project,
                                             installer=fresh, readiness=ready)
                except (ValueError, OSError, sqlite3.Error) as exc:
                    error = dict(type=type(exc).__name__, reason=str(exc))
                emit('empty-rehearsal-owner-crash', checkpoint=reached,
                     resume_error=error, stage_exists=Path(reached['stage']).exists())
                self.assertIsNone(error, 'a real empty-stage owner publication/retirement crash must resume')
                self.assertEqual(selected.epoch, 1)
                self.unchanged(helper, case, before)

    def test_unproved_rehearsal_owner_never_reclaims_trial_or_unrelated_files(self):
        import laya_journal_recovery as recovery
        for kind in ('missing', 'prefix-with-trial', 'non-prefix', 'hardlink', 'symlink', 'unknown-file'):
            with self.subTest(kind=kind), fixture() as values:
                helper, case, reader, bridge, _, _, _ = values
                self.pending_initial(values, hot=True)
                canonical = Path(case.journal).read_bytes()
                side = Path(str(case.journal) + '-journal')
                rollback = side.read_bytes()

                def resume(pipe):
                    actual_write = recovery._write

                    def written(path, data):
                        result = actual_write(path, data)
                        path = Path(path)
                        if path.name == 'trial.sqlite-journal':
                            pipe.send(dict(event='checkpoint', stage=str(path.parent)))
                            os._exit(EXIT_AT_CHECKPOINT)
                        return result

                    with helper.full_authority(case, reader, bridge), \
                            mock.patch.object(recovery, '_write', side_effect=written):
                        helper.installer(case, reader, bridge)

                with child(resume) as (process, pipe):
                    reached = receive(pipe, event='checkpoint')
                    process.join(10)
                    self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
                stage = Path(reached['stage'])
                owner = stage / 'owner.json'
                raw = owner.read_bytes()
                if kind == 'missing':
                    owner.unlink()
                elif kind == 'prefix-with-trial':
                    owner.write_bytes(raw[:len(raw) // 2])
                elif kind == 'non-prefix':
                    owner.write_bytes(b'unrelated private metadata must survive')
                elif kind == 'hardlink':
                    os.link(owner, case.state / 'owner-hardlink-witness')
                elif kind == 'symlink':
                    saved = case.state / 'saved-rehearsal-owner.json'
                    owner.rename(saved)
                    owner.symlink_to(saved)
                else:
                    sentinel = stage / 'unrelated-private.txt'
                    sentinel.write_bytes(b'not a rehearsal output')
                    sentinel.chmod(0o600)

                def snapshot():
                    return {p.name: (p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_nlink,
                                    os.readlink(p) if p.is_symlink() else p.read_bytes())
                            for p in stage.iterdir()}

                expected = snapshot()
                error = None
                with self.live_rw_opens(case.journal) as writes, helper.full_authority(case, reader, bridge):
                    try:
                        helper.installer(case, reader, bridge)
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        error = dict(type=type(exc).__name__, reason=str(exc))
                emit('unproved-rehearsal-owner-negative', kind=kind, refusal=error,
                     live_rw_opens=writes, stage_unchanged=snapshot() == expected)
                self.assertIsNotNone(error)
                self.assertEqual(writes, [])
                self.assertEqual(snapshot(), expected)
                self.assertEqual(Path(case.journal).read_bytes(), canonical)
                self.assertEqual(side.read_bytes(), rollback)

    def test_actual_released_writer_opened_before_conversion_refuses_v5_before_dml(self):
        with fixture() as (helper, case, reader, bridge, installer, readiness, _):
            old = self.old_tree(case)
            before = self.snapshot(helper, case)
            script = r'''
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import memvane.journal.install_journal as old
import memvane.provider.adapters.local_embed_provider as provider
assert pathlib.Path(old.__file__).resolve().parent == pathlib.Path(sys.argv[1]).resolve()
actual_open = old._open
traces, opens = [], []
calls = dict(queue=0, provider=0)
def no_queue(*args, **kwargs):
    calls['queue'] += 1
    raise AssertionError('historical CAS reached queue authority')
def no_provider(*args, **kwargs):
    calls['provider'] += 1
    raise AssertionError('historical CAS reached provider authority')
old.ClaimQueue.open = no_queue
provider.ReviewedLocalQmdProvider = no_provider
def paused(path, *, write):
    result = actual_open(path, write=write)
    result[1].set_trace_callback(traces.append)
    if write and not opens:
        opens.append(True)
        print(json.dumps(dict(event='opened-v4', version=old.VERSION, module=old.__file__)), flush=True)
        assert sys.stdin.readline().strip() == 'converted'
    return result
old._open = paused
result = dict(wrapper_observes_api_exceptions=True, not_an_old_cli_exit_claim=True)
try:
    old.cas_mark_new_write(sys.argv[2], int(sys.argv[3]))
    result['cas'] = 'unexpected-success'
except old.JournalEvidenceError as exc:
    result['cas'] = str(exc)
try:
    old.inspect_journal(sys.argv[2])
    result['read'] = 'unexpected-success'
except old.JournalEvidenceError as exc:
    result['read'] = str(exc)
result['dml'] = sum(sql.lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE ', 'REPLACE ')) for sql in traces)
result['queue_calls'] = calls['queue']
result['provider_calls'] = calls['provider']
result['observed_boundaries'] = ['ClaimQueue.open', 'ReviewedLocalQmdProvider']
print(json.dumps(dict(event='result', **result)), flush=True)
'''
            process = subprocess.Popen([sys.executable, '-B', '-I', '-c', script, str(old),
                                        str(case.journal), str(before['bundle']['epoch'])],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       encoding='utf8', env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            try:
                self.assertTrue(select.select([process.stdout], [], [], 30)[0], 'old writer open barrier timed out')
                line = process.stdout.readline()
                if not line:
                    out, err = process.communicate(timeout=5)
                    self.fail('actual historical closure did not reach old _open: ' + err + out)
                opened = json.loads(line)
                self.assertEqual(opened['event'], 'opened-v4')
                self.assertEqual(opened['version'], 4)
                with helper.full_authority(case, reader, bridge):
                    selected = reader.convert_reader_v5(case.state, case.project,
                                        installer=installer, readiness=readiness)
                out, err = process.communicate('converted\n', timeout=30)
                self.assertEqual(process.returncode, 0, err)
                result = json.loads(out.strip())
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
            emit('actual-old-writer', released_commit=READER_BRIDGE_FIXTURE_COMMIT, opened=opened, result=result,
                 current_reader=helper.selection(selected))
            self.assertEqual(result['cas'], 'journal_schema_unknown')
            self.assertEqual(result['read'], 'journal_schema_unknown')
            self.assertEqual(result['dml'], 0)
            self.assertEqual((result['queue_calls'], result['provider_calls']), (0, 0))
            self.unchanged(helper, case, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
