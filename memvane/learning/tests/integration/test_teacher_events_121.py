"""1.0.21 E-1 and the logs summary: ``learning.teacher`` rows through the real event log.

The worker pass runs the real fake-CLI processes of ``TeacherFixture``; its
``_emit`` writes through ``diagnostics.api.emit`` into a private fixture state
root, so field filtering and the logs script read what an install would.
``learning.score``/``learning.promotion``/pass rows are synthetic, in the
shape of plan §12.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from unittest import mock

from memvane.diagnostics import api as diagnostics
from memvane.learning.context_learning import general_teacher
from memvane.learning.tests.fake_teacher_cli_121 import BODY, TeacherFixture

LOGS = Path(__file__).resolve().parents[4] / 'skills' / 'logs' / 'scripts' / 'logs.py'
SECRET_NAME = re.compile(r'(?i)token|passw|secret|credential|api[_-]?key|authori[sz]|cookie')
RECALL_EDIT_CARD_KEYS = {'path', 'sha'}  # edit_recall: {'path': row['path'], 'sha': row['hash']}


class TeacherEvents(TeacherFixture):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        self.patches.enter_context(mock.patch.object(
            general_teacher, '_emit',
            side_effect=lambda project, event, **fields: diagnostics.emit(
                event, state_root=self.state, **fields)))

    def rows(self, event=None):
        path = self.state / 'events.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        return [row for row in rows if event is None or row['event'] == event]

    def summary(self, *extra):
        result = subprocess.run([sys.executable, '-B', str(LOGS), '--state-root', str(self.state),
                                 *extra], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_e1_gold_rows_carry_ids_usage_and_cards_but_no_text(self):
        claude, _ = self.add('a1')
        codex, _ = self.add('b1', host='codex')
        self.scenario('codex', {'tools': [['command_execution', None], ['command_execution', None],
                                          ['mcp_tool_call', 'search']]})
        self.run_pass()
        gold = {row['request_id']: row for row in self.rows('learning.teacher')
                if row['outcome'] == 'gold'}
        self.assertEqual(set(gold), {'a1', 'b1'})
        for sample, host in ((claude, 'claude'), (codex, 'codex')):
            row = gold[sample['request_id']]
            # The same request id the capture wrote into the sample and its recall.edit row.
            self.assertEqual(row['request_id'], sample['request_id'])
            self.assertEqual((row['teacherHost'], row['isolation'], row['selectedCount']),
                             (host, 'ok', 1))
            expected = [{'path': 'wiki/card0.md', 'sha': sample['candidates'][0]['revision_sha256']}]
            self.assertEqual(row['gold'], expected)
            self.assertEqual(set(row['gold'][0]), RECALL_EDIT_CARD_KEYS)
            for key in ('usageIn', 'usageOut', 'usageCached', 'ms', 'model', 'tier', 'modelReported',
                        'layaCount', 'overlap', 'exact'):
                self.assertIn(key, row)
            self.assertGreater(row['usageIn'], 0)
            self.assertEqual([key for key in row if SECRET_NAME.search(key)], [])
            text = json.dumps(row, ensure_ascii=False)
            for secret in (sample['prompt']['text'], BODY, 'private-path', 'session-zq77',
                           'tooluse-zq88', '/secret/tool-argument', 'tool-output-text'):
                self.assertNotIn(secret, text)
        self.assertIn('usageReasoning', gold['b1'])
        self.assertIn('userInstructionsBytes', gold['b1'])
        self.assertNotIn('toolWarnings', gold['a1'])
        self.assertEqual(gold['b1']['toolWarnings'], [{'name': 'command_execution', 'count': 2},
                                                      {'name': 'mcp_tool_call:search', 'count': 1}])
        out = self.summary()
        self.assertRegex(out, r'teacher \d{4}-\d\d-\d\d: calls=2 failed=0 usageIn=\d+ usageOut=\d+')
        self.assertIn('teacher selected: 0=0 1=2 2=0 3=0', out)
        self.assertIn('teacher models: claude claude-haiku-5-5 1, codex gpt-6-luna 1', out)
        self.assertIn('WARNING teacher tool calls: 1 call(s); command_execution 2, '
                      'mcp_tool_call:search 1', out)

    def test_summary_reports_failures_waits_isolation_scores_and_promotions(self):
        emit = lambda event, **fields: self.assertTrue(
            diagnostics.emit(event, state_root=self.state, **fields))
        for rid, outcome, reason in (('r1', 'gold', 'single_pass'),
                                     ('r2', 'pending_review', 'gold_invalid_choice'),
                                     ('r3', 'pending_review', 'gold_invalid_choice'),
                                     ('r4', 'provider_refused', 'provider_quota_waiting'),
                                     ('r5', 'pending_review', 'gold_attempts_exhausted')):
            emit('learning.teacher', outcome=outcome, reason=reason, request_id=rid,
                 teacherHost='claude', model='claude-haiku-5-5', isolation='ok',
                 selectedCount=0 if outcome == 'gold' else None, usageIn=100, usageOut=5)
        emit('learning.teacher', outcome='pre_send_rejected', reason='teacher_isolation_incomplete',
             teacherHost='codex', isolation='refused')
        emit('learning.teacher', outcome='pending_review', reason='teacher_isolation_violated',
             request_id='r6', teacherHost='claude', isolation='violated')
        for wait in (None, 'teacher_host_unavailable', 'teacher_host_unavailable'):
            emit('learning.teacher', outcome='pass', reason='processed', calls=0, waitClaude=None,
                 waitCodex=wait, queuedClaude=0, queuedCodex=4)
        for exact in (0.4, 0.5, 0.6):
            emit('learning.score', checkpoint='cycle-2', cases=40, exactRate=exact, microF1=0.7,
                 emptyAgree=0.8, nonEmptyAgree=0.5, defaultRank=3, unknownSelection=1,
                 goldOutsideK=2)
        emit('learning.score', checkpoint='cycle-1', cases=12, exactRate=0.3, microF1=0.4,
             emptyAgree=0.5, nonEmptyAgree=0.2, defaultRank=0, unknownSelection=0, goldOutsideK=0)
        emit('learning.promotion', outcome='waiting', cycle='cycle-3', trainCases=100,
             evalCases=49, evalEmpty=9)
        emit('learning.promotion', outcome='rejected', reason='candidate_not_better',
             cycle='cycle-3', trainCases=100, evalCases=50, evalEmpty=10, candidateExact=0.5,
             activeExact=0.6)
        out = self.summary()
        self.assertRegex(out, r'calls=6 failed=5 \(gold_attempts_exhausted 1, gold_invalid_choice 2, '
                              r'provider_quota_waiting 1, teacher_isolation_violated 1\) '
                              r'usageIn=500 usageOut=25')
        self.assertIn('teacher selected: 0=1 1=0 2=0 3=0', out)
        self.assertIn('teacher pending_review: gold_attempts_exhausted 1, gold_invalid_choice 2, '
                      'teacher_isolation_violated 1', out)
        self.assertIn('teacher isolation: refused=1 violated=1', out)
        self.assertRegex(out, r'teacher wait codex: teacher_host_unavailable since \S+ \S+ '
                              r'[\d.]+h queued=4')
        self.assertNotIn('teacher wait claude', out)
        self.assertIn('score cycle-2: cases=40 exactRate=0.6 microF1=0.7 emptyAgree=0.8 '
                      'nonEmptyAgree=0.5 defaultRank=3 unknownSelection=1 goldOutsideK=2 '
                      'trend exactRate 0.4->0.6 (n=3)', out)
        self.assertIn('score cycle-1: cases=12 exactRate=0.3', out)
        self.assertRegex(out, r'promotion \S+ \S+ waiting cycle=cycle-3 trainCases=100 '
                              r'evalCases=49 evalEmpty=9\n')
        self.assertRegex(out, r'promotion \S+ \S+ rejected cycle=cycle-3 trainCases=100 '
                              r'evalCases=50 evalEmpty=10 candidateExact=0.5 activeExact=0.6 '
                              r'reason=candidate_not_better')
        self.assertNotIn('WARNING teacher tool calls', out)

    def test_e4_status_shows_resolved_models_not_the_policy_model(self):
        from memvane.learning.context_learning import shared
        self.add('c1', host='claude')
        self.run_pass()
        self.assertEqual(self.policy['goldModel'], 'gpt-6-luna')
        self.assertNotIn('gpt-6-luna', json.dumps(shared.status(self.root)))
        out = self.summary()
        self.assertIn('teacher models: claude claude-haiku-5-5 1', out)
        self.assertNotIn('gpt-6-luna', out)
