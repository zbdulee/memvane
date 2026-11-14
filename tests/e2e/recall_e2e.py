"""Selected recall end to end through ``hooks/run-hook`` and the query skill wrapper.

Each check onboards a fixture project with ``tests.support.install_builder``
(fixture provider, lexical recall) and then asks recall the way a host does:
``hooks/run-hook recall <host>`` in a child process with the hook payload on
stdin, plus ``skills/query/scripts/query.sh``. Sync passes run in this
process (``Install.run_sync``). Assertions are route states, injected
context, exit codes and the install's recall event lines; none reads a wall
clock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.case import FixtureCase  # noqa: E402
from tests.support.install_builder import build_install  # noqa: E402
from tests.support.project import make_project, settle_index  # noqa: E402

HOOK_TIMEOUT = 60
SANDBOX_KEYS = ('QMD_SANDBOX', 'CODEX_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'GEMINI_SANDBOX')


class RecallEndToEnd(FixtureCase):
    def onboard(self, *tokens):
        """A selected install whose wiki holds one verified card per token."""
        self.fixture = make_project(self.home, name='recall')
        for token in tokens:
            self.card(self.fixture, token)
        self.fixture.commit('fixture')
        self.target = build_install(self, self.fixture)
        return self.target

    def card(self, project, token):
        note = project.write_note('notes/' + token + '.md', 'Synthetic source for ' + token + '.\n')
        return project.write_card('concepts/' + token + '.md', title=token + ' card',
                                  body=token + ' verified body\n', sources=[note])

    def env(self):
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        for key in SANDBOX_KEYS:
            env.pop(key, None)
        return env

    def run_hook(self, cwd, prompt, host='claude'):
        """``hooks/run-hook recall <host>`` for a prompt in ``cwd``: ``(exit code, context, stderr)``."""
        payload = {'session_id': 'e2e-' + host, 'cwd': str(cwd), 'hook_event_name': 'UserPromptSubmit',
                   'prompt': prompt}
        result = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), 'recall', host],
                                input=json.dumps(payload), capture_output=True, text=True,
                                timeout=HOOK_TIMEOUT, env=self.env(), cwd=str(cwd))
        return result.returncode, self.context(result.stdout), result.stderr

    def context(self, stdout):
        if not stdout.strip():
            return ''
        body = json.loads(stdout)
        return (body.get('hookSpecificOutput') or {}).get('additionalContext', '')

    def recalls(self):
        return [event for event in self.target.events() if event.get('event') == 'recall']

    def last_recall(self, count_before):
        recalls = self.recalls()
        self.assertEqual(len(recalls), count_before + 1, recalls[count_before:])
        return recalls[-1]

    # -- linked worktree -------------------------------------------------------------
    def test_linked_worktree_reads_its_own_view_and_a_branch_switch_is_cold(self):
        self.onboard('kestrelprobe')
        linked = self.fixture.linked_worktree('side', 'side')
        self.card(linked, 'wrenlinkedprobe')
        # Before the owner publishes the linked view, the linked checkout answers nothing.
        seen = len(self.recalls())
        code, context, err = self.run_hook(linked.root, 'wrenlinkedprobe')
        self.assertEqual((code, context), (0, ''), err)
        cold = self.last_recall(seen)
        self.assertEqual(cold.get('outcome'), 'empty', cold)
        self.assertIn('laya_view_preparing', cold.get('notes') or [], cold)

        built = self.target.run_sync()
        self.assertEqual([item['root'] for item in built.get('worktrees') or []], [str(linked.root)], built)
        side = built['worktrees'][0]
        self.assertEqual(side['state'], 'indexed', built)
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                code, own, err = self.run_hook(linked.root, 'wrenlinkedprobe', host)
                self.assertEqual(code, 0, err)
                self.assertIn('generation ' + side['generation'], own)
                self.assertIn('concepts/wrenlinkedprobe.md', own)
        code, main, err = self.run_hook(self.fixture.root, 'wrenlinkedprobe')
        self.assertEqual(code, 0, err)
        self.assertNotIn('concepts/wrenlinkedprobe.md', main)
        self.assertNotIn(side['generation'], main)

        # A branch switch in the linked checkout is a cold view until the owner publishes it.
        linked.git('switch', '-q', '-c', 'other')
        settle_index(linked.root)
        seen = len(self.recalls())
        code, context, err = self.run_hook(linked.root, 'wrenlinkedprobe')
        self.assertEqual((code, context), (0, ''), err)
        self.assertIn('laya_view_preparing', self.last_recall(seen).get('notes') or [])
        switched = self.target.run_sync()
        other = switched['worktrees'][0]
        self.assertEqual(other['state'], 'indexed', switched)
        self.assertNotEqual(other['generation'], side['generation'])
        code, answer, err = self.run_hook(linked.root, 'wrenlinkedprobe')
        self.assertEqual(code, 0, err)
        self.assertIn('generation ' + other['generation'], answer)
        self.assertIn('concepts/wrenlinkedprobe.md', answer)

    # -- an empty wiki ----------------------------------------------------------------
    def test_recall_is_empty_after_the_last_card_is_retired(self):
        self.onboard('heronprobe')
        card = self.fixture.wiki / 'concepts' / 'heronprobe.md'
        code, context, err = self.run_hook(self.fixture.root, 'heronprobe')
        self.assertEqual(code, 0, err)
        self.assertIn('concepts/heronprobe.md', context)
        card.unlink()
        self.target.settle()
        seen = len(self.recalls())
        code, context, err = self.run_hook(self.fixture.root, 'heronprobe')
        self.assertEqual((code, context), (0, ''), err)
        event = self.last_recall(seen)
        self.assertEqual((event.get('outcome'), event.get('cards') or []), ('empty', []), event)

    # -- the query skill -----------------------------------------------------------------
    def test_query_skill_answers_from_the_selected_index(self):
        self.onboard('kestrelprobe', 'ospreyprobe')
        seen = len(self.recalls())
        result = subprocess.run(['bash', str(ROOT / 'skills' / 'query' / 'scripts' / 'query.sh'),
                                 str(self.fixture.root), 'kestrelprobe'],
                                capture_output=True, text=True, timeout=HOOK_TIMEOUT, env=self.env(),
                                cwd=str(self.fixture.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        context = self.context(result.stdout)
        self.assertIn('concepts/kestrelprobe.md', context)
        self.assertNotIn('concepts/ospreyprobe.md', context)
        self.assertEqual(self.last_recall(seen).get('outcome'), 'served')


if __name__ == '__main__':
    unittest.main(verbosity=2)
