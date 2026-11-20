"""1.0.21 H: host routing, per-host limits and model resolution through fake CLIs."""
from __future__ import annotations

import json

from memvane.learning.context_learning import prequential, teacher_model_tiers as tiers
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture, edit_sample


class Hosts(TeacherFixture):
    def test_h1_each_case_uses_only_its_own_host_cli(self):
        self.add('cl', host='claude', tag='c')
        self.add('cx', host='codex', tag='x')
        self.run_pass()
        self.assertEqual(self.runs('claude'), 1)
        self.assertEqual(self.runs('codex'), 1)
        body = json.loads(self.records('claude', 'body')[0]['body'])
        self.assertIn('Field notes cl', body['message']['content'])
        self.assertIn('Field notes cx', self.records('codex')[0]['stdin'])
        self.assertNotIn('Field notes cx', self.records('claude', 'body')[0]['body'])

    def test_h2_missing_cli_rests_only_that_host(self):
        (self.bin / 'claude').unlink()
        self.add('cl', host='claude', tag='c')
        self.add('cx', host='codex', tag='x')
        result = self.run_pass()
        self.assertEqual(result['hosts'], {'claude': 'teacher_host_unavailable', 'codex': 'ready'})
        self.assertEqual(self.cases()['cl'][0], 'queued')
        self.assertEqual(self.cases()['cx'][0], 'gold')
        row = self.host_state()['hosts']['claude']
        self.assertEqual((row['reason'], row['queued']), ('teacher_host_unavailable', 1))
        self.assertIsNotNone(row['waitingSinceUnixMs'])

    def test_h3_resting_host_backlog_does_not_hide_other_host(self):
        (self.bin / 'claude').unlink()
        for index in range(100):
            self.add(f'cl{index:03d}', host='claude', tag=str(index))
        self.add('cx', host='codex', tag='x')
        self.run_pass()
        self.assertEqual(self.cases()['cx'][0], 'gold')
        self.assertEqual(self.host_state()['hosts']['claude']['queued'], 100)

    def test_h4_one_pass_makes_at_most_eight_calls(self):
        for index in range(20):
            self.add(f'a{index:02d}', tag=str(index))
        result = self.run_pass()
        self.assertEqual(self.runs('claude'), 8)
        self.assertEqual(result['calls'], 8)
        self.assertEqual(self.host_state()['hosts']['claude']['queued'], 12)

    def test_h5_refusal_mid_pass_continues_with_other_host(self):
        self.add('x1', host='codex', tag='1')
        self.add('x2', host='codex', tag='2')
        self.add('c1', host='claude', tag='3')
        self.scenario('codex', {'mode': 'quota'})
        self.run_pass()
        self.assertEqual(self.runs('codex'), 1)
        self.assertEqual(self.cases()['x2'][0], 'queued')
        self.assertEqual(self.cases()['c1'][0], 'gold')

    def test_h6_claude_model_resolution(self):
        self.add('ok', tag='o')
        self.scenario('claude', {'resolvedModel': 'claude-haiku-5-5-20261001'})
        self.run_pass()
        self.assertEqual(self.cases()['ok'][0], 'gold')
        for index, scenario in enumerate([{'resolvedModel': 'claude-sonnet-5-5'},
                                          {'resolvedModel': None},
                                          {'resolvedModel': 'claude-haiku-5-5-latest'}]):
            with self.subTest(scenario=scenario):
                name = f'bad{index}'
                self.add(name, tag=name)
                self.scenario('claude', scenario)
                bodies = len(self.records('claude', 'body'))
                result = self.run_pass()
                self.assertEqual(len(self.records('claude', 'body')), bodies)
                self.assertEqual(result['hosts']['claude'], tiers.UNRESOLVED)
                self.assertEqual(self.cases()[name][0], 'queued')
                self.assertEqual(self.stage(name), 'pre_send_rejected')
                self.assertEqual(self.host_state()['hosts']['claude']['reason'], tiers.UNRESOLVED)
                self.scenario('claude', {})
                self.run_pass()
                self.assertEqual(self.cases()[name][0], 'gold')

    def test_h6_codex_catalog_resolution(self):
        cases = [
            ('missing', None),
            ('malformed', 'not json'),
            ('no-list', {'models': {}}),
            ('hidden', {'models': [{'slug': 'gpt-6-luna', 'visibility': 'hide',
                                    'supported_reasoning_levels': [{'effort': 'low'}]}]}),
            ('no-low', {'models': [{'slug': 'gpt-6-luna', 'visibility': 'list',
                                    'supported_reasoning_levels': [{'effort': 'high'}]}]}),
            ('outside', {'models': [{'slug': 'gpt-6-sol', 'visibility': 'list',
                                     'supported_reasoning_levels': [{'effort': 'low'}]}]}),
        ]
        main = self.user / '.codex' / 'models_cache.json'
        good = main.read_text()
        for index, (name, value) in enumerate(cases):
            with self.subTest(name=name):
                self.add(name, host='codex', tag=name)
                if value is None:
                    main.unlink()
                    # A per-profile copy is never read in place of the main catalog.
                    self.catalog(name='models_cache.work.json')
                else:
                    main.write_text(value if isinstance(value, str) else json.dumps(value))
                    main.chmod(0o600)
                result = self.run_pass()
                self.assertEqual(result['hosts']['codex'], tiers.UNRESOLVED)
                self.assertEqual(self.runs('codex'), 0)
                self.assertEqual(self.cases()[name][0], 'queued')
                main.write_text(good)
                main.chmod(0o600)
                self.run_pass()
                self.assertEqual(self.cases()[name][0], 'gold')
                self.assertEqual([r['argv'][r['argv'].index('--model') + 1] for r in self.records('codex')][-1],
                                 'gpt-6-luna')
                (self.logs / 'calls.jsonl').unlink()

    def test_h6_codex_prefers_table_order(self):
        self.catalog([{'slug': 'gpt-5.6-luna', 'visibility': 'list',
                       'supported_reasoning_levels': [{'effort': 'low'}]}])
        self.add('x', host='codex')
        self.run_pass()
        argv = self.records('codex')[0]['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'gpt-5.6-luna')
        self.assertEqual(json.loads(self.cases()['x'][5])['teacher'],
                         {'host': 'codex', 'model': 'gpt-5.6-luna', 'tier': 1})

    def test_h7_gold_and_event_name_host_model_tier(self):
        self.add('cl', host='claude', tag='c', selected=1)
        self.add('cx', host='codex', tag='x', selected=0)
        self.run_pass()
        events = {e['request_id']: e for e in self.teacher_events('gold')}
        self.assertEqual((events['cl']['teacherHost'], events['cl']['model'], events['cl']['tier'],
                          events['cl']['modelReported']), ('claude', 'claude-haiku-5-5', 1, True))
        self.assertEqual((events['cx']['teacherHost'], events['cx']['model'], events['cx']['tier'],
                          events['cx']['modelReported']), ('codex', 'gpt-6-luna', 1, False))
        self.assertEqual(events['cl']['gold'][0]['path'], 'wiki/card0.md')
        self.assertEqual((events['cl']['layaCount'], events['cl']['overlap'], events['cl']['exact'],
                          events['cl']['goldOutsideK']), (1, 1, True, 0))
        self.assertEqual((events['cx']['layaCount'], events['cx']['overlap'], events['cx']['exact']),
                         (0, 0, False))
        self.assertEqual(events['cx']['catalogAgeDays'] >= 0, True)
        self.assertEqual((events['cx']['usageIn'], events['cx']['usageCached'], events['cx']['usageOut'],
                          events['cx']['usageReasoning']), (900, 100, 12, 4))
        passes = self.teacher_events('pass')
        self.assertEqual(len(passes), 1)
        self.assertEqual((passes[0]['calls'], passes[0]['gold'], passes[0]['quotaRemaining']), (2, 2, 80))
        for event in self.events:
            text = json.dumps(event)
            self.assertNotIn('Field notes', text)
            self.assertNotIn('cobalt garden', text)
            self.assertFalse(any('token' in key.lower() for key in event))

    def test_h8_logged_out_host_waits_and_is_not_called_again_this_pass(self):
        self.add('a1', host='codex', tag='1')
        self.add('a2', host='codex', tag='2')
        self.scenario('codex', {'mode': 'auth'})
        self.run_pass()
        self.assertEqual(self.runs('codex'), 1)
        self.assertEqual((self.cases()['a1'][0], self.cases()['a2'][0]), ('queued', 'queued'))
        self.assertEqual(self.host_state()['hosts']['codex']['reason'], 'teacher_provider_waiting')

    def test_quota_floor_rests_codex_and_records_remaining(self):
        self.add('x', host='codex', tag='x')
        self.add('c', host='claude', tag='c')
        self.scenario('codex', {'usedPercent': 75})
        result = self.run_pass()
        self.assertEqual(result['hosts']['codex'], 'deferred_quota')
        self.assertEqual(result['quotaRemaining'], 25)
        self.assertEqual(self.runs('codex'), 0)
        self.assertEqual(len(self.records('codex', 'app-server')), 1)
        self.assertEqual(self.cases()['c'][0], 'gold')
        self.assertEqual(self.teacher_events('pass')[0]['quotaRemaining'], 25)

    def test_no_codex_case_reads_no_quota(self):
        self.add('c', host='claude')
        self.run_pass()
        self.assertEqual(len(self.records('codex', 'app-server')), 0)


class Annotation(TeacherFixture):
    def test_gold_annotation_counts_against_capture(self):
        sample, _ = edit_sample('a', count=12, selected=2)
        ids = sample['sampling']['eligible_ids']
        self.assertEqual(prequential.gold_annotation(sample, ids[:2]),
                         {'layaCount': 2, 'overlap': 2, 'exact': True, 'goldOutsideK': 0})
        self.assertEqual(prequential.gold_annotation(sample, [ids[1], ids[11]]),
                         {'layaCount': 2, 'overlap': 1, 'exact': False, 'goldOutsideK': 1})
        self.assertEqual(prequential.gold_annotation(sample, []),
                         {'layaCount': 2, 'overlap': 0, 'exact': False, 'goldOutsideK': 0})

    def test_admitted_skips_non_edit_and_before_cohort_rows(self):
        from memvane.learning.tests.fake_teacher_cli_121 import prompt_sample
        from memvane.learning.context_learning import shared
        self.add('edit', tag='e')
        sample, bodies = prompt_sample('prompt')
        self.add('prompt', sample=sample, bodies=bodies)
        self.add('early', captured_at=1, tag='early')
        self.grant['cohort']['notBeforeUnixMs'] = 500_000
        with shared._db(self.root) as db:
            rows = prequential.admitted(db, self.project, self.grant)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual([row[1][1] for row in rows], ['edit'])
        self.assertEqual(len(rows[0][1]), 13)
        self.assertNotIn('project_cohort_assignments', tables)


if __name__ == '__main__':
    import unittest
    unittest.main()
