"""Real temp choice records and deterministic two-process update interleavings.

No product result is mocked. _current's return is only paused after its actual
read; flock is observed then delegated to the real OS primitive. The second
writer's lock-attempt/done signal releases the first, so a future lock cannot
deadlock this fixture. All waits and owned-child cleanup are finite.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

from memvane.host import onboarding_choice as choice
from tests.support.case import FixtureCase

ROOT = Path(__file__).resolve().parents[4]
WORKER = __name__  # the worker re-enters this module with -m


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def worker(role, operation, project, home):
    real_current = choice._current
    real_flock = choice.fcntl.flock
    paused = False

    def current(*args, **kwargs):
        nonlocal paused
        value = real_current(*args, **kwargs)
        emit({'event': 'read', 'role': role, 'value': value})
        if role == 'first' and not paused:
            paused = True
            if sys.stdin.readline().strip() != 'release':
                raise RuntimeError('fixture_release_missing')
        return value

    def flock(fd, flags):
        if role == 'second' and flags & choice.fcntl.LOCK_EX:
            emit({'event': 'lock_attempt', 'role': role})
        return real_flock(fd, flags)

    with mock.patch.object(choice, '_current', current), \
            mock.patch.object(choice.fcntl, 'flock', flock):
        if operation == 'skip':
            result = choice.skip(project, role, home=home)
        elif operation == 'never':
            result = choice.never(project, home=home)
        elif operation == 'resume':
            result = choice.resume(project, home=home)
        else:
            raise ValueError(operation)
    emit({'event': 'done', 'role': role, 'result': result})


class ChoiceBoundaries(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        settings = self.project / '.memvane' / 'settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({'name': 'onboarding', 'skipPaths': []}),
                            encoding='utf-8')

    @staticmethod
    def session(index):
        # Exactly the largest accepted identifier (ASCII, no escaping shortcut).
        return ('session-%04d-' % index).ljust(128, 'z')

    def test_110_legal_long_skips_remain_readable_and_keep_the_first(self):
        sessions = [self.session(index) for index in range(110)]
        for session in sessions:
            self.assertEqual(len(session), 128)
            self.assertEqual(choice.valid_session(session), session)
            result = choice.skip(str(self.project), session, home=self.home)
            self.assertEqual(result['status'], 'onboarding_notice_skipped')
        path = choice.record_path(self.project, self.home)
        value = json.loads(path.read_text(encoding='utf-8'))
        status = choice.status(str(self.project), home=self.home)
        emit({'case': '110-long-skips', 'bytes': path.stat().st_size,
              'readerLimit': choice.MAX_BYTES, 'rawSessions': len(value['skippedSessions']),
              'status': status, 'firstSuppressed': choice.suppressed(
                  self.project, sessions[0], home=self.home)})
        self.assertLessEqual(path.stat().st_size, choice.MAX_BYTES,
                             'the writer must never exceed its own reader bound')
        self.assertNotIn('reason', status)
        self.assertEqual(status['skippedSessions'], 110)
        self.assertTrue(all(choice.suppressed(self.project, session, home=self.home)
                            for session in sessions))

    def test_declared_512_session_capacity_fits_the_reader_at_worst_id_size(self):
        self.assertEqual(choice.MAX_SESSIONS, 512)
        # A public skip writes the final (512th) row. Seed only the preceding
        # 511 legal rows to keep this capacity check small; no 512 fsync loop.
        now = choice._now_ms()
        sessions = [self.session(index) for index in range(choice.MAX_SESSIONS)]
        seed = {'schema': choice.SCHEMA, 'projectRoot': str(self.project),
                'never': False, 'skippedSessions': [
                    {'session': session, 'at': now} for session in sessions[:-1]]}
        # _write is the real canonical writer; this deliberately probes whether
        # the declared legal state can be read back by the product at all.
        choice._write(self.project, seed, home=self.home)
        seed_path = choice.record_path(self.project, self.home)
        seed_bytes = seed_path.stat().st_size
        choice.skip(str(self.project), sessions[-1], home=self.home)
        size = seed_path.stat().st_size
        value = json.loads(seed_path.read_text(encoding='utf-8'))
        emit({'case': 'max-session-capacity', 'seedBytes': seed_bytes,
              'bytes': size, 'readerLimit': choice.MAX_BYTES,
              'declaredSessions': choice.MAX_SESSIONS,
              'rawSessions': len(value['skippedSessions'])})
        self.assertLessEqual(size, choice.MAX_BYTES)
        self.assertEqual(len(value['skippedSessions']), choice.MAX_SESSIONS)
        self.assertEqual(choice.status(str(self.project), home=self.home)
                         ['skippedSessions'], choice.MAX_SESSIONS)
        self.assertTrue(choice.suppressed(self.project, sessions[0], home=self.home))
        self.assertTrue(choice.suppressed(self.project, sessions[-1], home=self.home))

    def race(self, first_operation, second_operation):
        deadline = time.monotonic() + 15
        children, events = [], []
        environment = {**os.environ, 'HOME': str(self.home),
                       'PYTHONDONTWRITEBYTECODE': '1', 'QMD_SETUP_GUARD_FIXTURE': '1'}

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise AssertionError('two-process choice deadline exceeded')
            return seconds

        def start(role, operation):
            child = subprocess.Popen(
                [sys.executable, '-B', '-m', WORKER, '--worker',
                 role, operation, str(self.project), str(self.home)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', env=environment, bufsize=1, cwd=str(ROOT))
            children.append(child)
            return child

        def until(child, wanted):
            # Binary-free unbuffered event reads avoid TextIO read-ahead hiding
            # a second already-ready line from select(). Each event is tiny.
            fd = child.stdout.fileno()
            buffer = b''
            while True:
                ready, _, _ = select.select([fd], [], [], remaining())
                if not ready:
                    raise AssertionError('choice child made no progress')
                chunk = os.read(fd, 1)
                if not chunk:
                    raise AssertionError('choice child exited before signal: %r' % events)
                if chunk != b'\n':
                    buffer += chunk
                    continue
                event = json.loads(buffer.decode('utf-8'))
                buffer = b''
                events.append(event)
                if event['event'] in wanted:
                    return event['event']

        choice.skip(str(self.project), 'seed', home=self.home)
        try:
            first = start('first', first_operation)
            until(first, {'read'})
            second = start('second', second_operation)
            # Base: second completes its write. With a real fixed flock:
            # second reports its attempt before blocking behind first.
            signal = until(second, {'lock_attempt', 'done'})
            first.stdin.write('release\n')
            first.stdin.flush()
            first_out, first_err = first.communicate(timeout=remaining())
            second_out, second_err = second.communicate(timeout=remaining())
            for child, out, err in ((first, first_out, first_err),
                                    (second, second_out, second_err)):
                self.assertEqual(child.returncode, 0, out + err)
                for line in out.splitlines():
                    events.append(json.loads(line))
            path = choice.record_path(self.project, self.home)
            record = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
            emit({'case': 'two-process', 'firstOperation': first_operation,
                  'secondOperation': second_operation, 'releaseAfter': signal,
                  'pids': [child.pid for child in children], 'events': events,
                  'record': record})
            return record, events, signal
        finally:
            # Only these Popen-owned children can be signalled; reap both before
            # temp HOME removal, including assertion/timeout/error paths.
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()

    def test_two_process_distinct_skips_do_not_lose_an_update(self):
        record, events, signal = self.race('skip', 'skip')
        reads = [event for event in events if event['event'] == 'read']
        if signal == 'done':
            # Prove the base counterexample used the same initial value.
            self.assertEqual(reads[0]['value'], reads[1]['value'])
        self.assertEqual({row['session'] for row in record['skippedSessions']},
                         {'seed', 'first', 'second'})
        self.assertTrue(choice.suppressed(self.project, 'first', home=self.home))
        self.assertTrue(choice.suppressed(self.project, 'second', home=self.home))

    def test_never_and_skip_share_the_same_project_serialization(self):
        record, _events, _signal = self.race('never', 'skip')
        self.assertTrue(record['never'])
        self.assertEqual({row['session'] for row in record['skippedSessions']},
                         {'seed', 'second'})

    def test_resume_cannot_be_overwritten_by_an_earlier_paused_skip(self):
        record, _events, signal = self.race('skip', 'resume')
        # Once writers serialize, first owns its update before second resumes.
        # The base has no lock: resume completes and the stale first resurrects
        # the record, which is precisely the forbidden lost resume.
        emit({'case': 'resume-order', 'releaseAfter': signal, 'record': record})
        self.assertIsNone(record)
        self.assertFalse(choice.suppressed(self.project, 'first', home=self.home))

    def test_oversized_encoded_write_preserves_the_original_record(self):
        choice.skip(str(self.project), 'seed', home=self.home)
        path = choice.record_path(self.project, self.home)
        before = path.read_bytes()
        names = sorted(path.parent.iterdir())
        with self.assertRaisesRegex(ValueError, 'onboarding_choice_too_large'):
            choice._write(self.project, {'padding': '\uac00' * choice.MAX_BYTES},
                          home=self.home)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(path.parent.iterdir()), names)
        self.assertTrue(choice.suppressed(self.project, 'seed', home=self.home))

    def test_readers_create_no_directory_or_lock(self):
        self.assertFalse(choice.suppressed(self.project, 'seed', home=self.home))
        self.assertEqual(choice.status(str(self.project), home=self.home)['choice'], 'ask')
        self.assertFalse((self.home / '.local').exists())

    def test_unsafe_lock_and_non_posix_writers_fail_closed(self):
        choice.skip(str(self.project), 'seed', home=self.home)
        path = choice.record_path(self.project, self.home)
        lock = path.with_suffix('.lock')
        before = path.read_bytes()
        outside = self.home / 'outside'
        outside.write_bytes(b'outside remains untouched')
        outside.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'directory', 'public-mode'):
            with self.subTest(kind=kind):
                lock.unlink()
                if kind == 'symlink':
                    lock.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, lock)
                elif kind == 'directory':
                    lock.mkdir()
                else:
                    lock.write_bytes(b'')
                    lock.chmod(0o644)
                try:
                    with self.assertRaises((OSError, ValueError)):
                        choice.never(str(self.project), home=self.home)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(outside.read_bytes(), b'outside remains untouched')
                finally:
                    if lock.is_dir() and not lock.is_symlink():
                        lock.rmdir()
                    else:
                        lock.unlink()
                    lock.write_bytes(b'')
                    lock.chmod(0o600)
        with mock.patch.object(choice, 'fcntl', None):
            with self.assertRaisesRegex(ValueError, 'onboarding_choice_lock_unavailable'):
                choice.never(str(self.project), home=self.home)
        self.assertEqual(path.read_bytes(), before)

    def test_os_waiter_rejects_a_replaced_lock_inode(self):
        choice.skip(str(self.project), 'seed', home=self.home)
        path = choice.record_path(self.project, self.home)
        before = path.read_bytes()
        lock = path.with_suffix('.lock')
        deadline = time.monotonic() + 15
        child = None
        try:
            with choice._choice_lock(self.project, self.home):
                child = subprocess.Popen(
                    [sys.executable, '-B', '-m', WORKER, '--worker',
                     'second', 'skip', str(self.project), str(self.home)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', cwd=str(ROOT), env={**os.environ,
                        'PYTHONDONTWRITEBYTECODE': '1', 'QMD_SETUP_GUARD_FIXTURE': '1'})
                buffer = b''
                while True:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, 'waiter never attempted the real flock')
                    ready, _, _ = select.select([child.stdout.fileno()], [], [], remaining)
                    self.assertTrue(ready)
                    byte = os.read(child.stdout.fileno(), 1)
                    self.assertTrue(byte, 'waiter exited before flock attempt')
                    if byte != b'\n':
                        buffer += byte
                        continue
                    event = json.loads(buffer.decode('utf-8'))
                    buffer = b''
                    if event['event'] == 'lock_attempt':
                        break
                replacement = lock.with_name('replacement.lock')
                replacement.write_bytes(b'')
                replacement.chmod(0o600)
                original = lock.stat()
                os.replace(replacement, lock)
                self.assertNotEqual(lock.stat().st_ino, original.st_ino)
            out, err = child.communicate(timeout=max(0.01, deadline - time.monotonic()))
            emit({'case': 'replaced-lock-waiter', 'pid': child.pid,
                  'returncode': child.returncode, 'stdout': out, 'stderr': err})
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('onboarding_choice_lock_changed', err)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(choice.suppressed(self.project, 'seed', home=self.home))
        finally:
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(*sys.argv[2:])
    else:
        unittest.main()
