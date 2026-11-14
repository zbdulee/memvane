"""The logs skill's unified view (``skills/logs/scripts/logs.py --all``).

Contract: ``logs.py events --all [--details] [--json] [--limit N]`` merges every
registered target (``--register``, re-checked against the current route) in UTC
order, labels each row with root/install/host, reports a target it cannot read
with its reason, and splits one 16 MiB read budget equally so no target starves
another. Default text and ``--json`` never print raw diagnostic text or base64
parts (orphans included); only ``--details`` prints reassembled text,
terminal-escaped. The single-install view keeps its output.

The script is loaded once in process (the one sanctioned script load of the
test tree). Synthetic data only: every state root is a private directory under
the fixture HOME, the registry is the product's own file under
``XDG_CONFIG_HOME`` written by ``--register``, and only the route lookup
(``journal.api.select_route``) answers synthetic selected decisions.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.case import FixtureCase  # noqa: E402

LOGS = ROOT / 'skills' / 'logs' / 'scripts' / 'logs.py'
LOG_NAME = 'events.jsonl'
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
SECRETS = [SENTINEL, 'INCOMPLETESENTINEL', 'ORPHANSENTINEL']


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


def write_log(path, rows, mode='wb'):
    with path.open(mode) as stream:
        for row in rows:
            stream.write(json.dumps(row).encode() + b'\n')
    os.chmod(path, 0o600)


class UnifiedLogs(FixtureCase):
    def setUp(self):
        super().setUp()
        saved = os.environ.get('XDG_CONFIG_HOME')
        self.addCleanup(lambda: os.environ.__setitem__('XDG_CONFIG_HOME', saved) if saved is not None
                        else os.environ.pop('XDG_CONFIG_HOME', None))
        os.environ['XDG_CONFIG_HOME'] = str(self.home / '.config')
        self.logs = runpy.run_path(str(LOGS), run_name='memvane_logs_skill')
        self.routes, self.targets, self.files = {}, [], {}
        for i in range(9):
            host = HOSTS[i % 3]
            rows = [{'ts': f'2026-11-09T00:00:{i * 2:02d}Z', 'event': 'recall',
                     'outcome': 'served', 'host': host, 'reason': f'first{i}'},
                    {'ts': f'2026-11-09T00:01:{i * 2 + 1:02d}Z', 'event': 'hook.error',
                     'outcome': 'error', 'host': host, 'reason': f'second{i}'}]
            if i == 0:
                rows[1:1] = diagnostic_rows('2026-11-09T00:00:30Z')
                rows[-1]['diagnostic_id'] = DIAG_ID
                rows[0]['prompt'] = {'sha256': '0' * 64, 'chars': 1, 'text': 'OLD-PROMPT-SENTINEL'}
            for row in rows:
                row.update(v=1, install=f'install-synthetic-{i}', host=host)
            target = self.target(f'proj{i}', f'install-synthetic-{i}', rows)
            target['host'] = host
            self.targets.append(target)
        self.stale = self.target('stale', 'install-stale', [])
        self.locked = self.target('locked', 'install-locked', [{'v': 1, 'ts': '2026-11-09T00:05:00Z',
                                                                 'event': 'recall', 'outcome': 'served'}])
        roots = [t['root'] for t in self.targets] + [self.stale['root'], self.locked['root']]
        code, out, err = self.run_logs(*[arg for root in roots for arg in ('--register', root)])
        self.assertEqual(code, 0, out + err)
        # After registration the stale project's route names another install, and the
        # locked install's current log cannot be opened.
        self.routes[self.stale['root']]['installId'] = 'install-replaced'
        locked_log = Path(self.locked['state_root']) / LOG_NAME
        os.chmod(locked_log, 0)
        self.addCleanup(os.chmod, locked_log, 0o600)

    def target(self, name, install, rows):
        root, state = self.tmp / name, self.tmp / ('state-' + name)
        root.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        path = state / LOG_NAME
        write_log(path, rows)
        self.files[path] = path.read_bytes()
        self.routes[str(root)] = {'state': 'candidate', 'projectRoot': str(root), 'installId': install,
                                  'journalPath': str(state / 'journal.sqlite')}
        return {'root': str(root), 'install_id': install, 'state_root': str(state)}

    def route(self, root, action='recall', **_kwargs):
        decision = self.routes.get(str(root))
        return dict(decision) if decision else {'state': 'legacy'}

    def run_logs(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('memvane.journal.api.select_route', side_effect=self.route), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.logs['main'](list(argv))
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

    def statuses(self, out):
        return {row['target']['root']: row['target'] for row in self.json_rows(out) if 'target' in row}

    def assert_no_raw(self, text):
        self.assertNotIn('OLD-PROMPT-SENTINEL', text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for data in self.files.values():
            for row in self.json_rows(data.decode()):
                if row.get('event') == 'diagnostic.part':
                    self.assertNotIn(row['data'], text)

    # -- targets ------------------------------------------------------------------------
    def test_registered_targets_are_rechecked_against_the_current_route(self):
        code, out, err = self.run_logs('events', '--all', '--json', '--limit', '500')
        statuses = self.statuses(out)
        self.assertEqual(statuses[self.stale['root']]['status'], 'stale_route', out)
        self.assertEqual(statuses[self.locked['root']]['status'], 'read_failed', out)
        self.assertEqual({statuses[t['root']]['status'] for t in self.targets}, {'ok'}, out)
        self.assertEqual(code, 0, err)

    def test_unreadable_current_log_is_not_hidden_by_readable_rotation(self):
        path = Path(self.locked['state_root']) / LOG_NAME
        rotated = path.with_name(path.name + '.1')
        write_log(rotated, [{'v': 1, 'ts': '2026-11-09T00:04:00Z', 'event': 'recall', 'reason': 'rotated'}])
        with self.assertRaises(PermissionError):
            os.open(path, os.O_RDONLY)
        code, out, err = self.run_logs('events', '--all', '--json', '--limit', '500')
        self.assertEqual(self.statuses(out)[self.locked['root']]['status'], 'read_failed', out)
        self.assertIn('first1', out)
        self.assertEqual(code, 0, err)

    def test_an_absent_current_log_with_a_readable_rotation_is_read(self):
        state = Path(self.targets[4]['state_root'])
        os.replace(state / LOG_NAME, state / (LOG_NAME + '.1'))
        code, out, err = self.run_logs('events', '--all', '--json', '--limit', '500')
        self.assertEqual(self.statuses(out)[self.targets[4]['root']]['status'], 'ok', out)
        self.assertIn('first4', out)
        self.assertEqual(code, 0, err)

    def test_partial_registration_is_not_hidden_by_successful_view(self):
        code, out, err = self.run_logs('--all', '--register', str(self.tmp / 'synthetic-missing'))
        self.assertEqual(code, 1, err)
        self.assertIn('missing', out)

    # -- merged view ----------------------------------------------------------------------
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
        rows = [r for r in self.json_rows(out) if r.get('ts') and r.get('install') != 'install-locked']
        self.assertEqual({r.get('install') for r in rows}, {f'install-synthetic-{i}' for i in range(9)})
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

    def test_total_read_budget_is_split_so_no_target_starves(self):
        # Two targets each hold more than an equal share of the 16 MiB budget: each
        # loses only its oldest rows, every other target is read in full.
        pad = 'x' * 1000
        for index in (0, 1):
            path = Path(self.targets[index]['state_root']) / LOG_NAME
            rows = [{'v': 1, 'ts': '2026-11-08T00:%02d:%02dZ' % (n // 60 % 60, n % 60), 'event': 'recall',
                     'outcome': 'served', 'reason': f'pad{index}-{n}', 'note': pad,
                     'install': self.targets[index]['install_id']} for n in range(3000)]
            write_log(path, rows)
            path.write_bytes(path.read_bytes() + self.files[path])
        code, out, err = self.run_logs('events', '--all', '--json', '--limit', '100000')
        self.assertEqual(code, 0, err)
        statuses = self.statuses(out)
        readable = [s for s in statuses.values() if s['status'] in ('ok', 'read_failed')]
        self.assertEqual(len(readable), 10, statuses)
        share = BUDGET // len(readable)
        for index in (0, 1):
            status = statuses[self.targets[index]['root']]
            self.assertTrue(status.get('budget_truncated'), status)
            self.assertGreater(status['bytes'], share)
            self.assertIn(f'second{index}', out)
            self.assertIn(f'pad{index}-2999"', out)
            self.assertNotIn(f'pad{index}-0"', out)
        for target in self.targets[2:]:
            status = statuses[target['root']]
            self.assertNotIn('budget_truncated', status)
            self.assertEqual(status['events'], 2, status)

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
            if path.parent != Path(self.locked['state_root']):
                self.assertEqual(path.read_bytes(), data)

    def test_limit_keeps_newest_merged_rows(self):
        code, out, err = self.run_logs('events', '--all', '--json', '--limit', '3')
        self.assertEqual(code, 0, err)
        rows = [r for r in self.json_rows(out) if r.get('ts')]
        self.assertEqual([r.get('reason') for r in rows], ['second6', 'second7', 'second8'])

    def test_single_install_output_unchanged(self):
        state = self.targets[5]['state_root']
        code, out, err = self.run_logs('events', '--state-root', state)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f'state root: {state}')
        self.assertTrue(lines[1].startswith('log files: events.jsonl '))
        self.assertEqual(lines[2:], ['2026-11-09 00:00:10 recall served reason=first5',
                                     '2026-11-09 00:01:11 hook.error error reason=second5'])
        code, out, err = self.run_logs('events', '--state-root', state, '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.json_rows(out), self.json_rows((Path(state) / LOG_NAME).read_text()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
