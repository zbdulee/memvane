"""The diagnostics read facades over rows the real event writers produced.

Rows come from ``api.emit`` and ``diagnostic_trace.capture`` into a fixture
state root and are read back with ``event_log.read``. The one exception is the
stored ``prompt.text`` of older installs, which no current writer produces: that
row is appended as those releases wrote it.
"""
from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unicodedata
from unittest import mock

from memvane.diagnostics import api, diagnostic_trace, event_log
from tests.support.case import FixtureCase

LOGS = Path(__file__).resolve().parents[4] / 'skills' / 'logs' / 'scripts' / 'logs.py'
_UNSAFE = ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')


def _safe(text):
    return not any(unicodedata.category(ch) in _UNSAFE for ch in text)


class EventQueryTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.tmp / 'project'
        (self.project / '.memvane').mkdir(parents=True)
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project),
                         'journalPath': str(self.state / 'journal.sqlite'),
                         'installId': 'a' * 32}

    def emit(self, event, **fields):
        self.assertTrue(api.emit(event, state_root=self.state, install='a' * 32, host='claude', **fields))

    def old_release_prompt_row(self):
        row = {'v': 1, 'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
               'install': 'a' * 32, 'event': 'recall', 'outcome': 'served',
               'prompt': {'sha256': 'f' * 64, 'chars': 6, 'text': 'SECRET'}}
        with open(self.state / event_log.NAME, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(row) + '\n')

    def capture(self, marker):
        try:
            raise RuntimeError(marker)
        except RuntimeError as exc:
            return diagnostic_trace.capture(self.decision, event='recall', reason='synthetic_failure',
                                            exc=exc, consent=lambda decision: True)

    def corpus(self):
        evil = 'boom\x1b[31m‮evil\nfake 2020-01-01 recall served'
        for index, semantic in enumerate(('complete', 'partial', 'partial', 'lexical_only', 'provider_absent')):
            self.emit('recall', outcome='served', ms=100 * (index + 1), semantic=semantic,
                      vectorSource='arena' if index < 3 else 'store', vectorCache='ok',
                      vectorCoveredMembers=4, vectorEligibleMembers=4 if index == 1 else 5,
                      semanticMs=10 + index, lexicalMs=5, selectorMs=0, session='s1',
                      cards=[{'path': 'w/a.md', 'sha': 'ab' * 32}], count=1,
                      degraded=evil if index == 0 else None)
        self.emit('gate', outcome='refused', reason='budget', severity='warning')
        self.emit('hook.error', outcome='error', reason='boom')
        self.emit('wake.sync', outcome='failed', reason='target_deferred', ms=10)
        ident = self.capture('MARKER-TEXT')
        self.assertIsNotNone(ident)
        self.emit('recall', outcome='refused', reason='core_failed', diagnostic_ids=[ident])
        self.old_release_prompt_row()
        return ident, evil

    def test_visibility_hides_parts_and_stored_prompt_text(self):
        ident, _evil = self.corpus()
        raw = event_log.read(self.state)
        self.assertTrue(any(row.get('event') == 'diagnostic.part' for row in raw))
        shown = api.query_events(raw, raw=True)
        self.assertFalse(any(row.get('event') == 'diagnostic.part' for row in shown))
        header = next(row for row in shown if row.get('event') == 'diagnostic.header')
        self.assertEqual(header['diagnostic_id'], ident)
        self.assertEqual(set(header) - {'ts', 'v', 'install', 'host', 'session', 'event', 'outcome', 'reason',
                                        'ms', 'dropped', 'diagnostic_id', 'kind', 'parts', 'bytes',
                                        'diagnostic_version', 'sha256', 'truncated', 'source_event'}, set())
        self.assertNotIn('MARKER-TEXT', json.dumps(shown))
        self.assertNotIn('SECRET', json.dumps(shown))
        old = shown[-1]
        self.assertEqual(old['prompt'], {'sha256': 'f' * 64, 'chars': 6})

    def test_views_and_window(self):
        self.corpus()
        rows = api.query_events(event_log.read(self.state), raw=True)
        self.assertEqual(len(api.query_events(rows, 'recalls')), 7)
        errors = api.query_events(rows, 'errors')
        # The warning refusal is not an error; hook.error and the failed/refused rows are.
        self.assertEqual(sorted(row['event'] for row in errors), ['hook.error', 'recall', 'wake.sync'])
        self.assertEqual([row['event'] for row in api.query_events(rows, 'events', event='gate')], ['gate'])
        later = datetime.now(timezone.utc) + timedelta(hours=3)
        self.assertEqual(api.query_events(rows, 'events', hours=1, now=later), [])
        self.assertEqual(len(api.query_events(rows, None, hours=24)), len(rows))
        with self.assertRaises(ValueError):
            api.query_events(rows, 'summary')

    def test_reassembly_is_explicit_and_reports_defects(self):
        ident, _evil = self.corpus()
        raw = event_log.read(self.state)
        result = api.reassemble_diagnostic(raw, ident)
        self.assertEqual((result['status'], result['diagnostic_id'], result['kind']),
                         ('complete', ident, 'exception'))
        self.assertIn('MARKER-TEXT', result['text'])
        without_part = [row for row in raw if not (row.get('event') == 'diagnostic.part'
                                                   and row.get('index') == 0)]
        self.assertEqual(api.reassemble_diagnostic(without_part, ident),
                         {'status': 'incomplete', 'diagnostic_id': ident})
        self.assertEqual(api.reassemble_diagnostic(raw, 'not-an-id')['status'], 'incomplete')
        self.assertEqual(api.reassemble_diagnostic(raw, 'f' * 32)['status'], 'incomplete')
        with mock.patch.object(diagnostic_trace, 'reassemble', side_effect=RuntimeError('broken')):
            self.assertEqual(api.reassemble_diagnostic(raw, ident),
                             {'status': 'unavailable', 'diagnostic_id': ident})

    def test_summary_lines_escape_and_match_the_logs_skill(self):
        _ident, evil = self.corpus()
        rows = api.query_events(event_log.read(self.state), raw=True)
        before = copy.deepcopy(rows)
        lines = api.summarize_events(rows, None)
        self.assertEqual(rows, before)
        self.assertTrue(all(_safe(line) for line in lines))
        self.assertIn('recall p50=', '\n'.join(lines))
        self.assertIn('semantic=3/5 (60.0%) fullCoverage=2/5', '\n'.join(lines))
        degraded = next(line for line in lines if line.startswith('degraded recalls: '))
        self.assertIn('\\x1b[31m\\u202eevil\\nfake', degraded)
        self.assertNotIn(evil, '\n'.join(lines))
        logs = runpy.run_path(str(LOGS), run_name='memvane_logs_skill')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            logs['summary'](rows, SimpleNamespace(hours=None))
        self.assertEqual(out.getvalue().splitlines(), lines)

    def test_slo_lines_count_partial_and_arena_coverage(self):
        self.corpus()
        rows = api.query_events(event_log.read(self.state), raw=True)
        listing = sorted((path.name, path.stat().st_size) for path in self.state.iterdir())
        lines = {line['line']: line for line in api.slo_lines(rows, 24, state_root=self.state)}
        semantic = lines['semantic']
        # Asked: complete, partial (covered), partial (short), lexical_only; provider_absent is not asked.
        self.assertEqual((semantic['n'], semantic['partial']), (4, 2))
        self.assertEqual((semantic['complete'], semantic['fullCoverage']), (0.75, 0.5))
        # The lexical-only recall read no vectors: the arena share is over the three sourced recalls.
        self.assertEqual((semantic['arena'], semantic['arenaComplete'], semantic['arenaFullCoverage']),
                         (1.0, 1.0, 0.6667))
        self.assertEqual(lines['install']['size'], 'unknown')
        text = api.slo_lines(rows, 24, state_root=self.state, text=True)
        self.assertEqual(len(text), len(lines))
        self.assertTrue(all(line.startswith('slo ') and _safe(line) for line in text))
        self.assertEqual(sorted((path.name, path.stat().st_size) for path in self.state.iterdir()), listing)
