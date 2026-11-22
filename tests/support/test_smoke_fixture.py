"""The smoke fixture against the product: its plan names what event rows record."""
from __future__ import annotations

import hashlib
import json
import uuid

from tests.support.case import FixtureCase
from tests.support import smoke_fixture


class SmokeFixture(FixtureCase):
    def built(self, **options):
        from tests.support.project import Project
        out = self.home / 'smoke'
        plan = smoke_fixture.build(out, run_id='abc123', **options)
        project = Project(out, plan['name'], [])
        project.git('init', '-q')
        project.commit('fixture')
        return out, plan, project

    def test_every_raw_document_has_a_trusted_card_and_no_file_is_left_without_one(self):
        out, plan, project = self.built()
        self.assertEqual(len(plan['cards']), 20)
        self.assertEqual(len({card['token'] for card in plan['cards']}), 20)
        tracked = sorted(project.git('ls-files').split())
        expected = sorted(['.gitignore', '.memvane/settings.json']
                          + [card['source'] for card in plan['cards']]
                          + [card['file'] for card in plan['cards']])
        self.assertEqual(tracked, expected)
        settings = json.loads((out / '.memvane/settings.json').read_text())
        self.assertEqual(settings['contextLearning'], {'capture': False, 'autoCycle': False})
        for card in plan['cards']:
            self.assertEqual(hashlib.sha256((out / card['file']).read_bytes()).hexdigest(), card['sha256'])
        # Each prompt of a session names a different card.
        self.assertEqual(len(set(plan['roles'].values())), len(smoke_fixture.ROLES))
        self.assertGreater(plan['prompts']['P2']['chars'], smoke_fixture.LONG_PROMPT_CHARS)
        self.assertNotIn(plan['roles']['short'], plan['prompts']['P2']['text'])

    def test_a_recall_event_records_the_plan_card_and_prompt_hash(self):
        from tests.support.install_builder import build_install
        _out, plan, project = self.built(cards=6)
        target = build_install(self, project)
        session = 's-' + uuid.uuid4().hex[:8]
        prompt = plan['prompts']['P1']
        code, out, err = target.hook('recall', {
            'session_id': session, 'cwd': str(project.root),
            'hook_event_name': 'UserPromptSubmit', 'prompt': prompt['text']})
        self.assertEqual(code, 0, err)
        card = next(item for item in plan['cards'] if item['slug'] == plan['roles']['short'])
        self.assertIn(card['token'], json.loads(out)['hookSpecificOutput']['additionalContext'])
        [row] = [row for row in target.events()
                 if row.get('event') == 'recall' and row.get('session') == session]
        self.assertEqual(row['prompt'], {'sha256': prompt['sha256'], 'chars': prompt['chars']})
        self.assertIn({'path': card['path'], 'sha': card['sha256']}, row['cards'], row['cards'])

    def test_the_delta_adds_changes_and_deletes_one_card_each(self):
        out, plan, project = self.built(cards=6)
        after = smoke_fixture.delta(out, plan)
        project.commit('delta')
        change = after['delta']
        self.assertTrue((out / change['added']['file']).is_file())
        self.assertFalse((out / change['deleted']['file']).exists())
        self.assertFalse((out / change['deleted']['source']).exists())
        self.assertEqual(change['changed']['before']['path'], change['changed']['after']['path'])
        self.assertNotEqual(change['changed']['before']['sha256'], change['changed']['after']['sha256'])
        for token in (change['added']['token'], change['changed']['after']['token'],
                      change['deleted']['token']):
            self.assertIn(token, after['prompts']['P4']['text'])
        self.assertIsNone(plan['delta'])
        with self.assertRaises(ValueError):
            smoke_fixture.delta(out, after)
