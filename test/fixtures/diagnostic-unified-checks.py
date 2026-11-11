"""Unified log viewing (diagnostic logging Task 5) - RED contract checks.

Contract: ``logs.py events --all [--details] [--json] [--limit N]`` merges every
target from ``log_targets.resolve()`` in UTC order, labels each row with
root/install/host, keeps per-install failures, and splits one 16 MiB read
budget fairly (``event_log.read(..., max_bytes=...)``). Default text and
``--json`` never print raw diagnostic text or base64 parts (orphans included);
only ``--details`` prints reassembled text, terminal-escaped. The single-install
call keeps its output.

Synthetic data only: every state root is a private temporary directory under
the temporary HOME from the Node wrapper. ``log_targets.resolve`` is mocked
in-process; no child process, network or real state is touched.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))
import event_log  # noqa: E402
import log_targets  # noqa: E402

BUDGET = 16 * 1024 * 1024
HOSTS = ('claude', 'codex', 'hermes')
DIAG_ID = 'a' * 32
INCOMPLETE_ID = 'b' * 32
ORPHAN_ID = 'c' * 32
SENTINEL = 'RAWSENTINEL-7f3e'
# ANSI clear-screen, carriage return and BEL try to hide earlier rows.
RAW_TEXT = f'Traceback {SENTINEL}\x1b[2J\x1b[31mred\r\x07tail-{SENTINEL}\n'
ORPHAN_RAW = b'ORPHANSENTINEL-91ab payload'
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


def diagnostic_rows(ts):
    raw = RAW_TEXT.encode('utf-8')
    parts = [raw[i:i + 180] for i in range(0, len(raw), 180)]
    rows = [{'ts': ts, 'event': 'diagnostic.part', 'outcome': 'part', 'reason': 'synthetic',
             'diagnostic_id': DIAG_ID, 'index': i, 'data': _b64(p)} for i, p in enumerate(parts)]
    rows.append({'ts': ts, 'event': 'diagnostic.header', 'outcome': 'exception',
                 'reason': 'synthetic', 'source_event': 'recall', 'kind': 'exception',
                 'diagnostic_version': 1, 'parts': len(parts), 'bytes': len(raw),
                 'sha256': hashlib.sha256(raw).hexdigest(), 'truncated': False,
                 'diagnostic_id': DIAG_ID})
    # Header claims two parts; only one exists -> incomplete.
    half = b'INCOMPLETESENTINEL-55'
    rows.append({'ts': ts, 'event': 'diagnostic.part', 'outcome': 'part', 'reason': 'synthetic',
                 'diagnostic_id': INCOMPLETE_ID, 'index': 0, 'data': _b64(half)})
    rows.append({'ts': ts, 'event': 'diagnostic.header', 'outcome': 'exception',
                 'reason': 'synthetic', 'source_event': 'recall', 'kind': 'exception',
                 'diagnostic_version': 1, 'parts': 2, 'bytes': len(half) * 2,
                 'sha256': '0' * 64, 'truncated': False, 'diagnostic_id': INCOMPLETE_ID})
    rows.append({'ts': ts, 'event': 'diagnostic.part', 'outcome': 'part', 'reason': 'synthetic',
                 'diagnostic_id': ORPHAN_ID, 'index': 0, 'data': _b64(ORPHAN_RAW)})
    return rows


SECRETS = [SENTINEL, 'INCOMPLETESENTINEL', 'ORPHANSENTINEL']


class Base(unittest.TestCase):
    def setUp(self):
        home = Path(os.environ.get('HOME', '')).resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        if Path.home().resolve() != home or tmp == home:
            self.fail('requires the temporary HOME from the Node wrapper')
        self.tmp = Path(tempfile.mkdtemp(prefix='unified-', dir=home))
        os.chmod(self.tmp, 0o700)
        self.targets, self.files = [], {}
        for i in range(9):
            root = self.tmp / f'proj{i}'
            state = self.tmp / f'state{i}'
            root.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            host = HOSTS[i % 3]
            rows = [{'ts': f'2026-11-09T00:00:{i * 2:02d}Z', 'event': 'recall',
                     'outcome': 'served', 'host': host, 'reason': f'first{i}'},
                    {'ts': f'2026-11-09T00:01:{i * 2 + 1:02d}Z', 'event': 'hook.error',
                     'outcome': 'error', 'host': host, 'reason': f'second{i}'}]
            if i == 0:
                rows[1:1] = diagnostic_rows('2026-11-09T00:00:30Z')
                rows[-1]['diagnostic_id'] = DIAG_ID
                rows[0]['prompt'] = {'sha256': '0' * 64, 'chars': 1,
                                     'text': 'OLD-PROMPT-SENTINEL'}
            for row in rows:
                row.update(v=1, install=f'install-synthetic-{i}', host=host)
            path = state / event_log.NAME
            path.write_bytes(b''.join(json.dumps(r).encode() + b'\n' for r in rows))
            os.chmod(path, 0o600)
            self.files[path] = path.read_bytes()
            self.targets.append({'root': str(root), 'install_id': f'install-synthetic-{i}',
                                 'state_root': str(state), 'host': host})
        self.stale = {'root': str(self.tmp / 'stale'), 'install_id': 'install-stale',
                      'refusal': 'stale_route'}
        locked = self.tmp / 'state-locked'
        locked.mkdir(mode=0o700)
        self.locked = {'root': str(self.tmp / 'locked'), 'install_id': 'install-locked',
                       'state_root': str(locked)}
        spec = importlib.util.spec_from_file_location(
            'memvane_logs_unified', REPO / 'skills/logs/scripts/logs.py')
        self.logs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.logs)
        self.reads = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_logs(self, *argv, extra=True):
        targets = list(self.targets) + ([self.stale, self.locked] if extra else [])
        real_read = event_log.read
        locked_root = self.locked['state_root']

        def read(state_root, *args, **kw):
            self.reads.append(kw.get('max_bytes', 'unset'))
            if str(state_root) == locked_root:
                raise PermissionError(13, 'synthetic permission denied')
            return real_read(state_root, *args, **kw)

        out, err = io.StringIO(), io.StringIO()
        code = None
        with patch.object(log_targets, 'resolve', lambda: [dict(t) for t in targets]), \
                patch.object(event_log, 'read', read), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.logs.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def json_rows(self, text):
        rows = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def assert_no_raw(self, text):
        self.assertNotIn('OLD-PROMPT-SENTINEL', text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for path in self.files:
            for row in self.json_rows(path.read_text()):
                if row.get('event') == 'diagnostic.part':
                    self.assertNotIn(row['data'], text)


class UnifiedChecks(Base):
    def test_unified_upgrades_preserves_stage_and_transition_display(self):
        path = Path(self.targets[0]['state_root']) / event_log.NAME
        rows = [
            {'v': 1, 'ts': '2026-11-09T00:02:00Z', 'event': 'bundle.upgrade',
             'outcome': 'backup.done', 'bytesDone': 5, 'bytesTotal': 9},
            {'v': 1, 'ts': '2026-11-09T00:03:00Z', 'event': 'bundle.upgrade',
             'outcome': 'complete', 'from': 'old-root', 'to': 'new-root'},
        ]
        with path.open('ab') as stream:
            for row in rows:
                stream.write(json.dumps(row).encode() + b'\n')
        code, out, err = self.run_logs('upgrades', '--all', extra=False)
        self.assertEqual(code, 0, err)
        self.assertIn('backup.done', out)
        self.assertIn('old-root -> new-root', out)
        self.assertNotIn('None -> None', out)

    def test_partial_registration_is_not_hidden_by_successful_view(self):
        failed = {'results': [{'requested': 'synthetic', 'status': 'missing'}], 'partial': True}
        with patch.object(log_targets, 'register', return_value=failed):
            code, out, err = self.run_logs('--all', '--register', 'synthetic')
        self.assertEqual(code, 1, err)
        self.assertIn('missing', out)

    def test_unified_default_lists_events_and_errors_can_show_details(self):
        code, out, err = self.run_logs('--all', '--limit', '500')
        self.assertEqual(code, 0, err)
        self.assertIn('first0', out)
        self.assertIn('second8', out)
        code, out, err = self.run_logs('errors', '--all', '--details', '--limit', '500')
        self.assertEqual(code, 0, err)
        self.assertIn(SENTINEL, out)

    def test_all_json_merges_nine_installs_chronologically_with_identity(self):
        code, out, err = self.run_logs('events', '--all', '--json', '--limit', '500')
        self.assertEqual(code, 0, err)
        rows = [r for r in self.json_rows(out) if r.get('ts')]
        installs = {r.get('install') for r in rows}
        self.assertEqual(installs, {f'install-synthetic-{i}' for i in range(9)})
        for r in rows:
            self.assertIn('root', r)
            self.assertIn(r.get('host'), HOSTS)
        stamps = [r['ts'] for r in rows]
        self.assertEqual(stamps, sorted(stamps))

    def test_all_text_lists_every_install_and_host(self):
        code, out, err = self.run_logs('events', '--all', '--limit', '500')
        self.assertEqual(code, 0, err)
        for t in self.targets:
            self.assertIn(t['install_id'], out)
            self.assertIn(t['root'], out)
        for host in HOSTS:
            self.assertIn(host, out)

    def test_total_read_budget_is_16_mib_split_fairly(self):
        code, _out, err = self.run_logs('events', '--all', '--json', '--limit', '500')
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(len(self.reads), 9)
        self.assertTrue(all(isinstance(b, int) and b > 0 for b in self.reads), self.reads)
        self.assertLessEqual(sum(self.reads), BUDGET)
        self.assertLessEqual(max(self.reads) - min(self.reads), len(self.reads))

    def test_malformed_route_and_unreadable_log_reported_others_survive(self):
        for args in (('events', '--all', '--limit', '500'),
                     ('events', '--all', '--json', '--limit', '500')):
            code, out, err = self.run_logs(*args)
            both = out + err
            self.assertIn(self.stale['root'], both)
            self.assertIn('stale_route', both)
            self.assertIn(self.locked['root'], both)
            self.assertRegex(both, r'(?i)unreadable|permission|denied|read_failed')
            self.assertIn('install-synthetic-8', both)
            self.assertIn(code, (0, 1), err)

    def test_default_text_and_json_never_print_raw_or_base64(self):
        for args in (('events', '--all', '--limit', '500'),
                     ('events', '--all', '--json', '--limit', '500'),
                     ('events', '--state-root', self.targets[0]['state_root'], '--json',
                      '--limit', '500')):
            code, out, err = self.run_logs(*args)
            self.assertEqual(code, 0, err)
            self.assert_no_raw(out + err)
            self.assertIn(DIAG_ID, out)

    def test_details_reveals_escaped_text_marks_incomplete_keeps_prior_rows(self):
        code, out, err = self.run_logs('events', '--all', '--details', '--limit', '500')
        self.assertEqual(code, 0, err)
        self.assertIn(SENTINEL, out)
        self.assertIn(f'tail-{SENTINEL}', out)
        self.assertIsNone(CONTROL.search(out), repr(CONTROL.search(out)))
        self.assertRegex(out, r'\\(x1b|u001b|e|033)')
        self.assertIn('incomplete', out)
        self.assertNotIn('INCOMPLETESENTINEL', out)
        self.assertNotIn('ORPHANSENTINEL', out)
        self.assertNotIn(_b64(ORPHAN_RAW), out)
        self.assertIn('first0', out)  # the row before the diagnostic stays visible
        for path, data in self.files.items():
            self.assertEqual(path.read_bytes(), data)

    def test_limit_keeps_newest_merged_rows(self):
        code, out, err = self.run_logs('events', '--all', '--json', '--limit', '3')
        self.assertEqual(code, 0, err)
        rows = [r for r in self.json_rows(out) if r.get('ts')]
        self.assertEqual([r.get('reason') for r in rows], ['second6', 'second7', 'second8'])

    def test_single_install_output_unchanged(self):
        state = self.targets[5]['state_root']
        code, out, err = self.run_logs('events', '--state-root', state, extra=False)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f'state root: {state}')
        self.assertTrue(lines[1].startswith('log files: events.jsonl '))
        self.assertEqual(lines[2:], ['2026-11-09 00:00:10 recall served reason=first5',
                                     '2026-11-09 00:01:11 hook.error error reason=second5'])
        code, out, err = self.run_logs('events', '--state-root', state, '--json', extra=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.json_rows(out), self.json_rows(
            (Path(state) / event_log.NAME).read_text()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
