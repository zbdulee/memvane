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
INITIAL = ('v5-locked', 'v5-sealed', 'v5-versioned', 'v5-committed')
COW = ('v5-image-copied', 'v5-image-committed', 'v5-image-fsynced',
       'v5-image-replaced', 'v5-directory-fsynced')


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
        listed = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '-r', '--name-only', '6437108'],
                                capture_output=True, encoding='utf8', timeout=30)
        self.assertEqual(listed.returncode, 0, 'local historical closure unavailable: ' + listed.stderr)
        names = listed.stdout.splitlines()
        wanted = [prefix for prefix in ('core', 'hooks', '.claude-plugin', '.codex-plugin')
                  if any(name.startswith(prefix + '/') for name in names)]
        wanted += [name for name in ('plugin.yaml', '__init__.py') if name in names]
        archived = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', '6437108', '--', *wanted],
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

    def test_actual_released_writer_opened_before_conversion_refuses_v5_before_dml(self):
        with fixture() as (helper, case, reader, bridge, installer, readiness, _):
            old = self.old_tree(case)
            before = self.snapshot(helper, case)
            script = r'''
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import laya_install_journal as old
import laya_local_embed_provider as provider
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
            process = subprocess.Popen([sys.executable, '-B', '-I', '-c', script, str(old / 'core'),
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
            emit('actual-old-writer', released_commit='6437108', opened=opened, result=result,
                 current_reader=helper.selection(selected))
            self.assertEqual(result['cas'], 'journal_schema_unknown')
            self.assertEqual(result['read'], 'journal_schema_unknown')
            self.assertEqual(result['dml'], 0)
            self.assertEqual((result['queue_calls'], result['provider_calls']), (0, 0))
            self.unchanged(helper, case, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
