import json
import os
from concurrent.futures import ThreadPoolExecutor

from tests.support.case import FixtureCase
from memvane.diagnostics import api
from memvane.diagnostics.job_telemetry import replay, SCHEMA


class JobTelemetryTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.directory = self.tmp / 'private'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'job-telemetry.jsonl'

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_concurrent_private_lines_exclude_content(self):
        def write(i):
            return api.telemetry_emit(self.path, 'queued', job_id='job:%d' % i,
                                      prompt='SECRET_PROMPT', body='SECRET_BODY')
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertTrue(all(pool.map(write, range(60))))
        rows = self.rows()
        self.assertEqual(len(rows), 60)
        self.assertEqual(len({row['job_id'] for row in rows}), 60)
        self.assertTrue(all(row['schema'] == SCHEMA for row in rows))
        self.assertNotIn('SECRET_', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_metrics_are_normalized_and_secrets_discarded(self):
        self.assertTrue(api.telemetry_emit(self.path, 'finished', job_id='one', status='success',
            resources={'avg_cpu_percent': '42', 'peak_cpu_percent': float('nan'),
                       'observed_peak_rss_kib': 1024, 'resource_samples': 0, 'secret': 'SENTINEL'}))
        self.assertTrue(api.telemetry_emit(self.path, 'finished', job_id='two', status='success',
            resources={'observed_avg_cpu_percent': 175, 'observed_peak_cpu_percent': 220,
                       'observed_avg_rss_kib': 2048, 'observed_peak_rss_kib': 4096,
                       'resource_samples': 3, 'elapsed_seconds': 1.25,
                       'before': {'available_mib': 8000, 'memory_pressure': {'system_free_percent': 36}},
                       'after': {'swap_used_mib': 110}}))
        unknown, measured = self.rows()
        for key in ('cpu_avg_percent', 'cpu_peak_percent', 'rss_avg_mib', 'rss_peak_mib'):
            self.assertIsNone(unknown[key])
        self.assertEqual([measured[k] for k in ('cpu_avg_percent', 'cpu_peak_percent', 'rss_avg_mib', 'rss_peak_mib', 'run_ms')], [175, 220, 2, 4, 1250])
        self.assertEqual(measured['system_before']['memory_free_percent'], 36)
        self.assertEqual(measured['system_after']['swap_used_mib'], 110)
        self.assertNotIn('SENTINEL', self.path.read_text())

    def test_unsafe_destinations_leave_victim_untouched(self):
        victim = self.directory / 'victim'
        victim.write_text('unchanged')
        self.path.symlink_to(victim)
        self.assertFalse(api.telemetry_emit(self.path, 'queued', job_id='safe'))
        public = self.directory / 'public'
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        self.assertFalse(api.telemetry_emit(public / self.path.name, 'queued', job_id='safe'))
        self.assertFalse(api.telemetry_emit(self.path, 'queued', job_id='bad/identifier'))
        self.assertEqual(victim.read_text(), 'unchanged')

    def test_replay_keeps_retries_and_rejects_bad_finish(self):
        for phase, attempt, stamp, extra in [('queued', 1, 1000, {}), ('started', 1, 1001, {}),
            ('finished', 1, 1003, {'status': 'error', 'error_code': 'provider_quota'}),
            ('queued', 2, 1004, {}), ('started', 2, 1005, {}),
            ('finished', 2, 1006, {'status': 'success', 'run_ms': 900})]:
            self.assertTrue(api.telemetry_emit(self.path, phase, job_id='job', attempt=attempt, ts=stamp, **extra))
        self.assertTrue(api.telemetry_emit(self.path, 'started', job_id='crashed', ts=10))
        lines = self.path.read_text().splitlines()
        for fields in ({'status': []}, {'status': 'success', 'rss_peak_mib': 'invalid'},
                       {'status': 'error', 'exit_code': '17'}):
            lines.append(json.dumps(dict(schema=SCHEMA, phase='finished', job_id='crashed', attempt=1, ts=12, **fields)))
        rows = replay(lines + ['not-json'])
        self.assertEqual([(r['attempt'], r['status'], r['queue_wait_ms'], r['run_ms']) for r in rows[:2]],
                         [(1, 'error', 1000, 2000), (2, 'success', 1000, 900)])
        self.assertEqual(rows[0]['error_code'], 'provider_quota')
        self.assertEqual(rows[2]['status'], 'incomplete')
        self.assertIsNone(rows[2]['finished_at'])
