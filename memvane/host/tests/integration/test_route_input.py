"""Hook classification of interrupted onboarding windows (``route_input``, ``run-hook``).

A recall in a project whose onboarding or activation stopped midway is told
``legacy`` (exit 0 without context); every other action keeps the refusal.
"""
from contextlib import redirect_stdout
import io
import json
import os
import subprocess
import unittest

from memvane.host import route_input
from memvane.host.tests.support import NOTE, PROMPT
from tests.support.case import FixtureCase
from tests.support.install_builder import ROOT, build_install
from tests.support.project import make_project


class InterruptedWindows(FixtureCase):
    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home)
        note = self.fixture.write_note('notes/heron.md', NOTE)
        self.fixture.write_card('heron.md', title='Emerald heron', body=NOTE, sources=[note])
        self.fixture.commit('fixture')
        self.project = self.fixture.root

    def payload(self, action):
        path = self.tmp / ('event-%s.json' % action)
        path.write_text(json.dumps({'session_id': 's-1', 'cwd': str(self.project), 'prompt': PROMPT,
                                    'hook_event_name': 'UserPromptSubmit'}), encoding='utf-8')
        return path

    def classify(self, action):
        out = io.StringIO()
        with redirect_stdout(out):
            code = route_input.main(['classify-hook', action, str(self.payload(action)),
                                     str(self.project)])
        return code, out.getvalue().strip()

    def run_hook(self, action):
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS'):
            env.pop(key, None)
        result = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), action, 'claude'],
                                input=self.payload(action).read_text(), capture_output=True,
                                text=True, timeout=60, env=env, cwd=str(ROOT))
        return result.returncode, result.stdout, result.stderr

    def assert_recall_quiet(self, reason=None):
        from memvane.journal.api import select_route
        decision = select_route(str(self.project), action='recall')
        self.assertEqual((decision['state'], decision.get('recallQuiet')), ('blocked', True), decision)
        if reason is not None:
            self.assertEqual(decision['reason'], reason)
        self.assertEqual(self.classify('recall'), (0, 'legacy'))

    def test_a_selection_marker_without_its_route_answers_recall_quietly(self):
        build_install(self, self.fixture)
        (self.project / '.memvane' / 'route.json').unlink()
        self.assert_recall_quiet('selection_route_missing')
        self.assertEqual(self.classify('gate'), (0, 'blocked'))
        self.assertEqual(self.run_hook('recall'), (0, '', ''))
        code, out, err = self.run_hook('gate')
        self.assertEqual((code, out), (3, ''))
        self.assertIn('request refused', err)

    def test_a_preparing_install_whose_preview_proof_fails_answers_recall_quietly(self):
        build_install(self, self.fixture, upto='prepared')
        self.fixture.write_settings(name='renamed')
        self.assert_recall_quiet()
        # Writers of a preparing install are not selected yet: no hook work.
        self.assertEqual(self.classify('gate'), (0, 'legacy'))
        self.assertEqual(self.run_hook('recall'), (0, '', ''))


if __name__ == '__main__':
    unittest.main()
