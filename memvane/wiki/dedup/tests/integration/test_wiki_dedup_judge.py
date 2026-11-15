"""The dedup judge over a fake host CLI: outcome branches, cooldowns and the paid-call cap.

Every judge here is ``tests.support.engines_fake.FakeEngines`` except the one
process test, which runs the real bundled claude adapter against a fake
``claude`` binary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import unittest
from unittest import mock

from memvane.settings import types as settings_types
from memvane.wiki.dedup import api as dedup
from memvane.wiki.dedup import wiki_dedup_judge as judge
from memvane.wiki.engines import api as engines
from memvane.wiki.workspace import api as workspace
from tests.support import settings_fake
from tests.support.case import FixtureCase
from tests.support.engines_fake import FakeEngines
from tests.support.project import make_project

PAGE_A = {'path': 'entities/a.md', 'content': 'The stalker asked for CCTV footage at the front desk.'}
PAGE_B = {'path': 'entities/b.md', 'content': 'At the front desk the stalker requested the CCTV video.'}
DUPLICATE = {'verdict': 'duplicate', 'reason': 'same event', 'sharedFacts': ['cctv'],
             'uniqueToA': ['only in A'], 'uniqueToB': ['only in B']}


class JudgeCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.compile_cfg = settings_fake.expanded(self.project)['compile']

    def fake(self, responses=None, **kwargs):
        return FakeEngines({'dedup': DUPLICATE} if responses is None else responses, **kwargs).install(self)

    def judge(self, engine='claude', compile_cfg=None):
        return dedup.judge_pair(self.root, compile_cfg or self.compile_cfg, PAGE_A, PAGE_B, engine)

    def cool_engine(self, key):
        self.assertTrue(engines.set_engine_cooldown(
            judge.engine_cooldown_path(self.root), key, 600,
            _queue_lock_path=workspace.queue_lock_path, write_text_atomic=workspace.write_text_atomic))


class Reach(JudgeCase):
    def test_fake_engines_reach_dedup_judge(self):
        seen = []

        def answer(payload, argv):
            seen.append(payload)
            return DUPLICATE
        fake = self.fake({'dedup': answer})
        verdict, info = self.judge('claude')
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual((fake.calls[0]['task'], fake.calls[0]['argv']),
                         ('dedup', engines.builtin_adapter_argv('claude')))
        self.assertEqual(verdict, 'duplicate')
        self.assertEqual({key: info[key] for key in ('outcome', 'verdict', 'engine', 'mode', 'producedBy',
                                                     'crossEngine', 'enginesAttempted', 'uniqueToA',
                                                     'uniqueToB', 'reason')},
                         {'outcome': 'ok', 'verdict': 'duplicate', 'engine': 'claude',
                          'mode': settings_types.VERIFIED_MODE_SELF, 'producedBy': 'claude',
                          'crossEngine': 'off', 'enginesAttempted': ['claude'],
                          'uniqueToA': ['only in A'], 'uniqueToB': ['only in B'], 'reason': 'same event'})
        payload = seen[0]
        self.assertEqual((payload['pageA'], payload['pageB'], payload['engine']), (PAGE_A, PAGE_B, 'claude'))
        self.assertEqual(payload['_qmd']['reasoningEffort'],
                         {'requested': 'medium', 'capabilityDeclared': True})

    def test_default_judging_uses_only_the_producer_with_every_host_configured(self):
        self.assertEqual(self.compile_cfg['extractor']['builtins'], ['claude', 'codex', 'hermes'])
        for producing in ('claude', 'codex', 'hermes'):
            with self.subTest(producing=producing):
                fake = self.fake()
                _, info = self.judge(producing)
                self.assertEqual([call['argv'] for call in fake.calls],
                                 [engines.builtin_adapter_argv(producing)])
                self.assertEqual((info['engine'], info['mode'], info['crossEngine']),
                                 (producing, 'self', 'off'))

    def test_page_bodies_are_cut_to_the_configured_size(self):
        seen = []
        self.fake({'dedup': lambda payload, argv: seen.append(payload) or DUPLICATE})
        long_a = {'path': 'a.md', 'content': 'x' * 2000}
        cfg = settings_fake.expanded(self.project, compile={'semanticDedup': {'judge': {'maxCharsPerPage': 10}}})
        dedup.judge_pair(self.root, cfg['compile'], long_a, PAGE_B, 'claude')
        self.assertEqual(len(seen[0]['pageA']['content']), 500, 'the floor is 500 characters')
        cfg = settings_fake.expanded(self.project, compile={'semanticDedup': {'judge': {'maxCharsPerPage': 900}}})
        dedup.judge_pair(self.root, cfg['compile'], long_a, PAGE_B, 'claude')
        self.assertEqual(len(seen[1]['pageA']['content']), 900)

    def test_each_verdict_value_is_returned(self):
        for verdict in ('duplicate', 'distinct', 'unclear'):
            with self.subTest(verdict=verdict):
                self.fake({'dedup': {'verdict': verdict, 'reason': 'r'}})
                got, info = self.judge()
                self.assertEqual((got, info['outcome'], info['verdict']), (verdict, 'ok', verdict))
                self.assertNotIn('unusableOutput', info)


class Unavailable(JudgeCase):
    def test_no_judge_means_no_call(self):
        table = [
            ('claude', {**self.compile_cfg, 'semanticDedup': {'judge': {'enabled': False}}}, 'judge_disabled'),
            ('', None, 'producing_engine_unavailable'),
            (settings_types.UNKNOWN_ENGINE, None, 'producing_engine_unavailable'),
            ('gemini', None, 'producing_engine_unavailable'),
            ('claude', {**self.compile_cfg, 'extractor': {'builtins': []}}, 'producing_engine_unavailable'),
        ]
        for engine, cfg, reason in table:
            with self.subTest(engine=engine, reason=reason):
                fake = self.fake()
                verdict, info = self.judge(engine, cfg)
                self.assertEqual((verdict, info['outcome'], info['reason']),
                                 (None, dedup.OUTCOME_UNAVAILABLE, reason))
                self.assertEqual(fake.calls, [])

    def test_the_kill_switch_spends_nothing(self):
        fake = self.fake()
        with mock.patch.dict(os.environ, {'QMD_DEDUP_JUDGE': 'off'}):
            verdict, info = self.judge()
        self.assertEqual((verdict, info['reason'], fake.calls), (None, 'judge_disabled', []))

    def test_an_absent_cli_costs_nothing_and_cools_nothing(self):
        fake = self.fake(returncode=127)
        verdict, info = self.judge()
        self.assertEqual((verdict, info['outcome'], info['reason']), (None, 'unavailable', 'cli_absent'))
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(judge.cooldown_path(self.root).exists())
        self.assertFalse(judge.engine_cooldown_path(self.root).exists())
        self.assertEqual(judge.diagnostics(info)['enginesAttempted'], ['claude'])


class Transient(JudgeCase):
    def test_a_cooled_producer_is_transient_without_a_call(self):
        self.cool_engine('claude')
        fake = self.fake()
        verdict, info = self.judge('claude')
        self.assertEqual((verdict, info['outcome'], info['reason'], info['enginesCooling']),
                         (None, 'transient', 'engines_cooling', ['claude']))
        self.assertEqual(fake.calls, [])
        # Another engine's cooldown does not hold this producer back.
        _, other = self.judge('codex')
        self.assertEqual((other['outcome'], len(fake.calls)), ('ok', 1))

    def test_a_paid_failure_cools_the_judge_so_the_next_pair_is_not_billed(self):
        for kwargs, reason in (({'timeout': True}, 'extractor_timeout'),
                               ({'returncode': 1}, 'extractor_failed')):
            with self.subTest(reason=reason):
                judge.cooldown_path(self.root).unlink(missing_ok=True)
                fake = self.fake(**kwargs)
                verdict, info = self.judge()
                self.assertEqual((verdict, info['outcome'], info['reason']), (None, 'transient', reason))
                self.assertNotIn('cooldownWriteFailed', info)
                self.assertTrue(judge.cooldown_active(self.root))
                again, info = self.judge()
                self.assertEqual((again, info['outcome'], info['reason']), (None, 'transient', 'cooldown'))
                self.assertEqual(len(fake.calls), 1, 'one paid call until the cooldown expires')

    def test_a_resource_deferral_is_transient_and_cools_nothing(self):
        with mock.patch('memvane.wiki.engines.api.run_extractor',
                        return_value=(None, 'resource_deferred', 75)) as run:
            verdict, info = self.judge()
        self.assertEqual((verdict, info['outcome'], info['reason'], run.call_count),
                         (None, 'transient', 'resource_deferred', 1))
        self.assertFalse(judge.cooldown_path(self.root).exists())

    def test_an_unbounded_cooldown_value_does_not_stop_the_judge_forever(self):
        path = judge.cooldown_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        for raw in ('1e300', 'inf', 'nan', 'garbage'):
            with self.subTest(raw=raw):
                path.write_text(raw + '\n', encoding='utf-8')
                self.assertFalse(judge.cooldown_active(self.root))
        fake = self.fake()
        self.assertEqual(self.judge()[0], 'duplicate')
        self.assertEqual(len(fake.calls), 1)

    def test_both_cooldown_writes_failing_is_surfaced(self):
        path = judge.cooldown_path(self.root)
        path.mkdir(parents=True)  # a directory where the expiry file belongs
        self.fake(timeout=True)
        verdict, info = self.judge()
        self.assertEqual((verdict, info['outcome'], info.get('cooldownWriteFailed')), (None, 'transient', True))
        self.assertTrue(judge.diagnostics(info)['cooldownWriteFailed'])


class UnusableOutput(JudgeCase):
    def test_unusable_output_is_suppressed_when_candidates_are_exhausted(self):
        table = [({'garbage': True}, None, 'invalid_extractor_json'),
                 ({}, {'dedup': {'verdict': 'pass'}}, 'invalid_verdict'),
                 ({}, {'dedup': {'reason': 'no verdict at all'}}, 'invalid_verdict')]
        for kwargs, responses, reason in table:
            with self.subTest(reason=reason, responses=responses):
                judge.cooldown_path(self.root).unlink(missing_ok=True)
                fake = self.fake(responses, **kwargs)
                verdict, info = self.judge()
                # A terminal unclear: the caller records a body-hash marker for the pair.
                self.assertEqual((verdict, info['outcome'], info['verdict'], info['reason'],
                                  info['unusableOutput']),
                                 ('unclear', dedup.OUTCOME_OK, 'unclear', reason, True))
                self.assertIn(reason, judge.UNUSABLE_OUTPUT_REASONS)
                # The judge is cooled too: a garbage adapter must not burn the scan's budget.
                self.assertTrue(judge.cooldown_active(self.root))
                self.assertEqual(self.judge()[1]['reason'], 'cooldown')
                self.assertEqual(len(fake.calls), 1)
                self.assertTrue(judge.diagnostics(info)['unusableOutput'])


FAKE_CLAUDE = r'''import json, sys
with open(%(log)r, 'a', encoding='utf-8') as log:
    log.write(json.dumps(sys.argv[1:]) + '\n')
sys.stdout.write('noise {"verdict":"pass"} more ' + json.dumps(%(answer)r))
'''


class ClaudeAdapterProcess(JudgeCase):
    """The bundled claude adapter answers the dedup task with the dedup prompt and verdict."""

    def test_dedup_task_through_the_real_adapter(self):
        log = self.tmp / 'claude-argv.jsonl'
        answer = {'verdict': 'distinct', 'reason': 'different events', 'uniqueToB': ['extra']}
        cli = self.tmp / 'claude'
        cli.write_text('#!%s\n' % sys.executable + FAKE_CLAUDE % {'log': str(log), 'answer': answer},
                       encoding='utf-8')
        os.chmod(cli, stat.S_IRWXU)
        with mock.patch.dict(os.environ, {'QMD_EXTRACTOR_CLAUDE_BIN': str(cli)}):
            verdict, info = dedup.judge_pair(self.root, self.compile_cfg,
                                             {'path': 'entities/a.md', 'content': 'BODY_A'},
                                             {'path': 'entities/b.md', 'content': 'BODY_B'}, 'claude')
        # The last valid dedup verdict wins; a verify-vocabulary object is not a dedup verdict.
        self.assertEqual((verdict, info['outcome'], info['reason'], info['uniqueToB']),
                         ('distinct', 'ok', 'different events', ['extra']))
        calls = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(calls), 1)
        prompt = calls[0][-1]
        for needle in ('BODY_A', 'BODY_B', '"duplicate"|"distinct"|"unclear"', '"reason"', 'uniqueToA',
                       'Being topically related is NOT being duplicate', 'Do NOT use any tools'):
            self.assertIn(needle, prompt)
        self.assertNotIn('REFUTE', prompt, 'not the verify prompt')
        self.assertNotIn('wiki candidates', prompt, 'not the extraction prompt')


if __name__ == '__main__':
    unittest.main()
