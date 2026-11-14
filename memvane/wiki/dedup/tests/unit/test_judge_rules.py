"""Dedup judge rules as input tables: who judges, when the judge is available, caps."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memvane.settings import types as settings_types
from memvane.wiki.dedup import wiki_dedup_judge as judge
from memvane.wiki.dedup import wiki_dedup_scan as scan

ALL_HOSTS = {'extractor': {'builtins': ['claude', 'codex', 'hermes']}}


class JudgeAttempts(unittest.TestCase):
    def test_the_producing_engine_judges_alone(self):
        for producing in ('claude', 'codex', 'hermes'):
            with self.subTest(producing=producing):
                attempts, policy, reason = judge.plan_judge_attempts(ALL_HOSTS, producing)
                self.assertEqual(len(attempts), 1, 'one paid call per pair, never a second host')
                self.assertEqual({key: attempts[0][key] for key in ('engine', 'mode', 'key')},
                                 {'engine': producing, 'mode': settings_types.VERIFIED_MODE_SELF,
                                  'key': producing})
                self.assertTrue(attempts[0]['argv'])
                self.assertEqual((policy, reason), (judge.CROSS_ENGINE_POLICY, ''))
                self.assertEqual(policy, 'off')

    def test_an_unknown_or_unresolvable_producer_has_no_attempt(self):
        table = [
            ('', ALL_HOSTS),
            (settings_types.UNKNOWN_ENGINE, ALL_HOSTS),
            ('gemini', ALL_HOSTS),            # not a declared builtin
            ('claude', {}),                   # no builtins at all
            ('claude', {'extractor': {'builtins': ['codex']}}),
        ]
        for producing, compile_cfg in table:
            with self.subTest(producing=producing, compile_cfg=compile_cfg):
                self.assertEqual(judge.plan_judge_attempts(compile_cfg, producing),
                                 ([], 'off', 'producing_engine_unavailable'))

    def test_a_cooled_producer_waits_and_another_cooled_engine_does_not_matter(self):
        self.assertEqual(judge.plan_judge_attempts(ALL_HOSTS, 'claude', cooled={'claude'}),
                         ([], 'off', 'engines_cooling'))
        attempts, _, reason = judge.plan_judge_attempts(ALL_HOSTS, 'claude', cooled={'codex', 'hermes'})
        self.assertEqual(([a['engine'] for a in attempts], reason), (['claude'], ''))

    def test_an_unresolvable_producer_is_unavailable_even_while_cooled(self):
        self.assertEqual(judge.plan_judge_attempts(ALL_HOSTS, 'gemini', cooled={'gemini'})[2],
                         'producing_engine_unavailable')


class Availability(unittest.TestCase):
    def test_config_alone_decides_availability(self):
        table = [
            (ALL_HOSTS, 'claude', True),
            (ALL_HOSTS, '', False),
            (ALL_HOSTS, settings_types.UNKNOWN_ENGINE, False),
            ({}, 'claude', False),
            ({**ALL_HOSTS, 'semanticDedup': {'judge': {'enabled': False}}}, 'claude', False),
            ({**ALL_HOSTS, 'semanticDedup': {'judge': {'enabled': True}}}, 'codex', True),
        ]
        for compile_cfg, producing, expected in table:
            with self.subTest(compile_cfg=compile_cfg, producing=producing):
                self.assertIs(judge.is_available(compile_cfg, producing), expected)

    def test_the_process_kill_switch_turns_the_judge_off(self):
        with mock.patch.dict(os.environ, {'QMD_DEDUP_JUDGE': 'off'}):
            self.assertFalse(judge.is_available(ALL_HOSTS, 'claude'))
            self.assertFalse(judge.is_enabled(ALL_HOSTS))


class Caps(unittest.TestCase):
    def test_paid_call_caps_come_from_the_budget(self):
        table = [
            ({}, judge.DEFAULT_MAX_PAIRS_PER_SCAN, judge.DEFAULT_MAX_PAIRS_PER_COMPILE),
            ({'budget': {'dedupPairsPerScan': 3, 'dedupPairsPerCompile': 2}}, 3, 2),
            ({'budget': {'dedupPairsPerScan': -4, 'dedupPairsPerCompile': -1}}, 0, 0),
            ({'budget': {'dedupPairsPerScan': 'x', 'dedupPairsPerCompile': None}},
             judge.DEFAULT_MAX_PAIRS_PER_SCAN, judge.DEFAULT_MAX_PAIRS_PER_COMPILE),
        ]
        for compile_cfg, scan_cap, compile_cap in table:
            with self.subTest(compile_cfg=compile_cfg):
                self.assertEqual((judge.max_pairs_per_scan(compile_cfg),
                                  judge.max_pairs_per_compile(compile_cfg)), (scan_cap, compile_cap))
        self.assertEqual((judge.DEFAULT_MAX_PAIRS_PER_SCAN, judge.DEFAULT_MAX_PAIRS_PER_COMPILE), (8, 1))


class Diagnostics(unittest.TestCase):
    def test_only_a_degrade_or_a_missing_verdict_leaves_fields(self):
        quiet = [
            {'outcome': 'ok', 'verdict': 'distinct', 'enginesAttempted': ['claude']},
            {'outcome': 'unavailable', 'reason': 'producing_engine_unavailable',
             'enginesAttempted': [], 'enginesCooling': []},
            {'outcome': 'unavailable', 'reason': 'judge_disabled'},
        ]
        for info in quiet:
            with self.subTest(info=info):
                self.assertEqual(judge.diagnostics(info), {})
        transient = {'outcome': 'transient', 'reason': 'extractor_timeout', 'engine': 'claude',
                     'mode': 'self', 'enginesAttempted': ['claude'], 'uniqueToA': ['dropped']}
        self.assertEqual(judge.diagnostics(transient),
                         {'outcome': 'transient', 'reason': 'extractor_timeout', 'engine': 'claude',
                          'mode': 'self', 'enginesAttempted': ['claude']})
        unusable = {'outcome': 'ok', 'verdict': 'unclear', 'reason': 'invalid_verdict',
                    'unusableOutput': True, 'enginesAttempted': ['claude']}
        self.assertTrue(judge.diagnostics(unusable).get('unusableOutput'))
        failed = {'outcome': 'transient', 'reason': 'cooldown', 'cooldownWriteFailed': True}
        self.assertTrue(judge.diagnostics(failed).get('cooldownWriteFailed'))


CARD = ('---\ntitle: "A"\nstatus: generated\ncreatedBy: memvane\n---\n\n'
        '> Auto-generated by MemVane from conversation/work context.\n\n'
        '<!-- qmd:auto:start id="main" sourceHash="abc123" -->\n## Summary\n'
        'The north bridge closes at 22:00.\n<!-- qmd:auto:end -->\n')


class BodyHash(unittest.TestCase):
    def test_the_hash_sees_only_the_prose(self):
        same = [
            CARD.replace('title: "A"', 'title: "Renamed"'),
            CARD.replace('sourceHash="abc123"', 'sourceHash="def456"'),
            CARD.replace('\n', '\r\n'),
            CARD.replace('22:00.', '22:00.   '),
            '---\ntitle: x\n---\nThe north bridge closes at 22:00.\n',
        ]
        for text in same:
            with self.subTest(text=text[:40]):
                self.assertEqual(scan.body_hash(text), scan.body_hash(CARD))
        self.assertNotEqual(scan.body_hash(CARD.replace('22:00', '23:00')), scan.body_hash(CARD))
        self.assertRegex(scan.body_hash(CARD), r'^[0-9a-f]{64}$')

    def test_skip_ledger_keeps_the_last_row_per_sorted_pair(self):
        rows = [
            '{"pageA":"b.md","pageB":"a.md","pageAHash":"hb1","pageBHash":"ha1"}',
            'not json',
            '{"pageA":"a.md","pageB":"c.md","pageAHash":"","pageBHash":"hc"}',
            '{"pageA":"a.md","pageB":"b.md","pageAHash":"ha2","pageBHash":"hb2"}',
            '{"pageA":"d.md","pageB":"c.md","pageAHash":"hd","pageBHash":"hc"}',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dedup-skipped.jsonl'
            path.write_text('\n'.join(rows) + '\n', encoding='utf-8')
            self.assertEqual(scan.load_skip_suppressions(path),
                             {('a.md', 'b.md'): ('ha2', 'hb2'), ('c.md', 'd.md'): ('hc', 'hd')})
            self.assertEqual(scan.load_skip_suppressions(Path(tmp) / 'absent.jsonl'), {})
        self.assertEqual(scan.load_skip_suppressions(None), {})
        self.assertEqual(scan.pair_key('b', 'a'), ('a', 'b'))


if __name__ == '__main__':
    unittest.main()
