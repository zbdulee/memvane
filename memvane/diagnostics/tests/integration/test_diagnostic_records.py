import json
import threading
import unittest
import os
from pathlib import Path
from unittest.mock import patch
from tests.support.case import FixtureCase
from memvane.diagnostics import api
from memvane.diagnostics import event_log


class DiagnosticRecordTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)

    def rows(self, path):
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_concurrent_successful_appends_survive_rotation(self):
        import subprocess
        import sys
        writers, per = 4, 100
        code = ('import json,sys; from memvane.diagnostics import api,event_log; '
                'event_log.LOG_LIMIT=70000; '
                'print(json.dumps([n for n in range(int(sys.argv[3])) '
                'if api.emit("c",state_root=sys.argv[1],writer=sys.argv[2],n=n,pad="x"*120)]))')
        children = [subprocess.Popen([sys.executable, '-B', '-c', code, str(self.state), str(i), str(per)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for i in range(writers)]
        written = set()
        for i, child in enumerate(children):
            out, err = child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0, err)
            written.update((str(i), n) for n in json.loads(out))
        rows = [(row['writer'], row['n']) for row in event_log.read(self.state) if row['event'] == 'c']
        self.assertTrue((self.state / 'events.jsonl.1').exists())
        self.assertEqual(len(rows), len(set(rows)))
        self.assertEqual(set(rows), written)

    def test_busy_rotation_lock_refuses_without_writing(self):
        import fcntl
        import subprocess
        import sys
        fd = os.open(self.state / 'events.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            code = ('import sys; from memvane.diagnostics import api; '
                    'print(api.emit("gate", state_root=sys.argv[1]))')
            result = subprocess.run([sys.executable, '-B', '-c', code, str(self.state)],
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'False')
            self.assertFalse((self.state / 'events.jsonl').exists())
        finally:
            os.close(fd)

    def test_refused_append_is_counted_then_reported_once(self):
        target = self.tmp / 'victim'
        target.write_text('keep')
        path = self.state / event_log.NAME
        path.symlink_to(target)
        with patch.object(event_log, '_DROPPED', 0):
            self.assertFalse(api.emit('gate', state_root=self.state))
            self.assertFalse(api.emit('gate', state_root=self.state))
            info = event_log.inspect(self.state)
            self.assertEqual((info['writable'], info['refused']), (False, 2))
            self.assertEqual(info['lastRefusal']['reason'], 'events_not_regular')
            self.assertEqual(target.read_text(), 'keep')
            path.unlink()
            self.assertTrue(api.emit('gate', state_root=self.state))
            self.assertTrue(api.emit('gate', state_root=self.state))
            first, second = event_log.read(self.state)
            self.assertEqual(first['dropped'], 2)
            self.assertNotIn('dropped', second)

    def test_busy_refusal_counter_does_not_block_emit(self):
        import fcntl
        import subprocess
        import sys
        (self.state / event_log.NAME).symlink_to(self.tmp / 'absent')
        fd = os.open(self.state / event_log.REFUSED_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            code = ('import sys; from memvane.diagnostics import api; '
                    'print(api.emit("gate", state_root=sys.argv[1]))')
            result = subprocess.run([sys.executable, '-B', '-c', code, str(self.state)],
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'False')
        finally:
            os.close(fd)

    def test_reader_prefers_newest_rows_within_byte_budget(self):
        for index in range(100):
            self.assertTrue(api.emit('old', state_root=self.state, n=index, pad='y' * 200))
        os.replace(self.state / event_log.NAME, self.state / (event_log.NAME + '.2'))
        for index in range(5):
            self.assertTrue(api.emit('new', state_root=self.state, n=index))
        with patch.object(event_log, 'READ_LIMIT', 2000):
            rows = event_log.read(self.state)
        self.assertEqual([row['event'] for row in rows][-5:], ['new'] * 5)
        self.assertLess(len(rows), 20)
        self.assertEqual(len(event_log.read(self.state)), 105)

    def test_cached_state_root_is_revalidated_for_changed_permissions(self):
        with patch.object(event_log, 'open_state_dir', wraps=event_log.open_state_dir) as walk:
            for _ in range(5):
                self.assertTrue(api.emit('x', state_root=self.state))
        self.assertEqual(walk.call_count, 1)
        self.state.chmod(0o755)
        self.assertFalse(api.emit('x', state_root=self.state))
        self.state.chmod(0o700)
        self.assertTrue(api.emit('x', state_root=self.state))

    def test_fifo_generation_is_skipped_by_reader(self):
        import subprocess
        import sys
        os.mkfifo(self.state / 'events.jsonl.1', 0o600)
        self.assertTrue(api.emit('gate', state_root=self.state))
        code = ('import json,sys; from memvane.diagnostics import event_log; '
                'print(json.dumps([r["event"] for r in event_log.read(sys.argv[1])]))')
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.state)],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['gate'])

    def test_hard_cap_refuses_until_rotation_lock_is_released(self):
        import fcntl
        fd = os.open(self.state / 'events.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            with patch.object(event_log, 'LOG_LIMIT', 2000):
                results = [api.emit('x', state_root=self.state, pad='z' * 100) for _ in range(200)]
            self.assertIn(False, results)
            self.assertLessEqual((self.state / 'events.jsonl').stat().st_size, 4000)
            self.assertEqual(event_log.inspect(self.state)['lastRefusal']['reason'], 'events_over_hard_cap')
            fcntl.flock(fd, fcntl.LOCK_UN)
            with patch.object(event_log, 'LOG_LIMIT', 2000):
                self.assertTrue(api.emit('after', state_root=self.state))
            self.assertTrue((self.state / 'events.jsonl.1').exists())
        finally:
            os.close(fd)

    def test_prompt_fields_store_only_digest_and_character_count(self):
        result = api.prompt_fields('private prompt')
        self.assertEqual(set(result), {'sha256', 'chars'})
        self.assertEqual(result['chars'], len('private prompt'))

    def test_event_writer_drops_secret_fields_and_bounds_prompt_metadata(self):
        self.assertTrue(api.emit('recall', outcome='ok', state_root=self.state,
                                 prompt=api.prompt_fields('private prompt'),
                                 api_key='secret', note='safe'))
        rows = self.rows(self.state / 'events.jsonl')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event'], 'recall')
        self.assertNotIn('api_key', rows[0])
        self.assertEqual(rows[0]['prompt'], api.prompt_fields('private prompt'))
        self.assertNotIn('private prompt', json.dumps(rows))

    def test_event_writer_refuses_missing_state_root_without_raising(self):
        self.assertFalse(api.emit('recall', state_root=None))
        self.assertEqual(event_log.inspect(self.tmp / 'missing')['reason'], 'state_root_missing')

    def test_job_telemetry_filters_unapproved_content_fields(self):
        path = self.state / 'job-telemetry.jsonl'
        self.assertTrue(api.telemetry_emit(path, 'queued', job_id='job-1',
                                           prompt='private', body='private', attempt=1))
        row = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(row['job_id'], 'job-1')
        self.assertNotIn('prompt', row)
        self.assertNotIn('body', row)

    def test_event_line_stays_within_the_format_limit(self):
        self.assertTrue(api.emit('large', state_root=self.state, detail='x' * 12000))
        line = (self.state / 'events.jsonl').read_bytes()
        self.assertLessEqual(len(line.rstrip(b'\n')), event_log.MAX_LINE)

    def test_reader_skips_malformed_records(self):
        path = self.state / 'events.jsonl'
        self.assertTrue(api.emit('valid', state_root=self.state))
        with path.open('ab') as stream:
            stream.write(b'{broken}\n')
        rows = event_log.read(self.state)
        self.assertEqual([row['event'] for row in rows], ['valid'])

    def test_wiki_tail_sanitizes_display_fields(self):
        api.wiki(self.state, 'created', 'wiki/card.md', 'Title\u202e unsafe')
        rendered = api.wiki_tail(self.state)
        self.assertIn('wiki/card.md', rendered)
        self.assertNotIn('\u202e', rendered)

    def test_job_replay_leaves_unfinished_attempt_open(self):
        from memvane.diagnostics import job_telemetry
        lines = [json.dumps({'schema': job_telemetry.SCHEMA, 'phase': 'started',
                             'ts': 1.0, 'job_id': 'job-1', 'attempt': 1})]
        rows = job_telemetry.replay(lines)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['finished_at'])

    def test_job_replay_separates_attempts_and_rejects_invalid_finishes(self):
        from memvane.diagnostics import job_telemetry
        path = self.state / 'job-telemetry.jsonl'
        emit = job_telemetry.emit
        emit(path, 'queued', job_id='episode:7:write', attempt=1, ts=1000)
        emit(path, 'started', job_id='episode:7:write', attempt=1, ts=1001)
        emit(path, 'finished', job_id='episode:7:write', attempt=1, status='error', ts=1003)
        emit(path, 'queued', job_id='episode:7:write', attempt=2, ts=1004)
        emit(path, 'started', job_id='episode:7:write', attempt=2, ts=1005)
        emit(path, 'finished', job_id='episode:7:write', attempt=2, status='success', run_ms=900, ts=1006)
        emit(path, 'started', job_id='crashed:1', ts=1011)
        bad = {'schema': job_telemetry.SCHEMA, 'phase': 'finished', 'job_id': 'crashed:1',
               'attempt': 1, 'ts': 1012, 'status': [], 'run_ms': 1000}
        rows = job_telemetry.replay(path.read_text().splitlines() + [json.dumps(bad), 'not-json'])
        self.assertEqual([(r['attempt'], r['retry_count'], r['status'], r['queue_wait_ms'], r['run_ms'])
                          for r in rows[:2]], [(1, 0, 'error', 1000, 2000),
                                               (2, 1, 'success', 1000, 900)])
        self.assertEqual(rows[2]['status'], 'incomplete')
        self.assertIsNone(rows[2]['finished_at'])

    def test_job_metrics_normalize_aliases_and_remove_secrets(self):
        path = self.state / 'job-telemetry.jsonl'
        self.assertTrue(api.telemetry_emit(path, 'finished', job_id='sample:1', op='train',
            status='success', resources={'observed_avg_cpu_percent': 175,
                'observed_peak_cpu_percent': 220, 'observed_avg_rss_kib': 2048,
                'observed_peak_rss_kib': 4096, 'resource_samples': 3,
                'elapsed_seconds': 1.25, 'secret': 'private'}))
        row = json.loads(path.read_text())
        self.assertEqual([row['cpu_avg_percent'], row['cpu_peak_percent'],
                          row['rss_avg_mib'], row['rss_peak_mib'], row['run_ms']],
                         [175, 220, 2, 4, 1250])
        self.assertNotIn('private', path.read_text())

    def test_append_serializes_concurrent_event_lines(self):
        workers = [threading.Thread(target=api.emit,
                                    kwargs={'event': 'parallel', 'state_root': self.state,
                                            'worker': index})
                   for index in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        rows = event_log.read(self.state)
        self.assertEqual(len(rows), 12)
        self.assertEqual({row['worker'] for row in rows}, set(range(12)))

    def test_event_symlink_is_refused_without_touching_target(self):
        target = self.tmp / 'outside.jsonl'
        target.write_text('untouched\n', encoding='utf-8')
        (self.state / 'events.jsonl').symlink_to(target)
        self.assertFalse(api.emit('refused', state_root=self.state))
        self.assertEqual(target.read_text(encoding='utf-8'), 'untouched\n')

    def test_rotation_keeps_bounded_private_generations_in_order(self):
        with patch.object(event_log, 'LOG_LIMIT', 2000):
            for index in range(200):
                event_log.emit('recall', state_root=self.state, n=index)
        names = sorted(path.name for path in self.state.iterdir()
                       if path.name.startswith(event_log.NAME))
        self.assertEqual(names, ['events.jsonl', 'events.jsonl.1', 'events.jsonl.2'])
        for name in names:
            info = (self.state / name).stat()
            self.assertLessEqual(info.st_size, 2000)
            self.assertEqual(info.st_mode & 0o777, 0o600)
        numbers = [row['n'] for row in event_log.read(self.state)]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers[-1], 199)

    def test_event_writer_catches_storage_errors(self):
        with patch.object(event_log, '_write', side_effect=RuntimeError('synthetic')):
            self.assertFalse(event_log.emit('gate', state_root=self.state))
        self.assertFalse(event_log.emit('gate', state_root='relative/path'))

    def test_replaced_state_root_is_rechecked(self):
        self.assertTrue(event_log.emit('first', state_root=self.state))
        old = self.tmp / 'state.old'
        os.rename(self.state, old)
        self.state.mkdir(mode=0o700)
        self.assertTrue(event_log.emit('second', state_root=self.state))
        self.assertEqual([row['event'] for row in event_log.read(self.state)], ['second'])
        self.assertEqual([row['event'] for row in event_log.read(old)], ['first'])

    def test_reader_limits_total_bytes_even_for_corrupt_generations(self):
        for name in (event_log.NAME, event_log.NAME + '.1', event_log.NAME + '.2'):
            (self.state / name).write_bytes(b'x' * 300000)
            os.chmod(self.state / name, 0o600)
        real_read = os.read
        sizes = []

        def count_read(fd, size):
            data = real_read(fd, size)
            sizes.append(len(data))
            return data

        with patch.object(event_log, 'READ_LIMIT', 200000), \
                patch.object(event_log.os, 'read', side_effect=count_read):
            self.assertEqual(event_log.read(self.state), [])
        self.assertLessEqual(sum(sizes), 200000)


if __name__ == '__main__':
    unittest.main()
