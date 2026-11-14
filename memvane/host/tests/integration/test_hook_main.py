"""Fail-safe boundary of the Python hook entries (``entry/run.py``).

A hook process answers with exit 0 (deny as stdout JSON) or, for a refused
selected route, exit 3 with a terse stderr line. An exception inside an entry
must never surface as exit 1 ("hook failed") or as partial stdout. Each case
runs the real ``entry/run.py`` in a child; the fault is injected in that child
before the entry module is imported.
"""
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest

from memvane.host.tests.support import PROMPT, HostInstallCase
from tests.support.case import FixtureCase
from tests.support.install_builder import ROOT

ENTRY = ROOT / 'entry' / 'run.py'
REFUSED = '[memvane] selected_proof_or_query_failed; request refused\n'


def run_entry(argv, *, payload=None, inject='', env=None, cwd=None):
    """``entry/run.py <argv>`` in a child with ``inject`` applied first."""
    script = '\n'.join(['import runpy, sys', 'from unittest import mock',
                        textwrap.dedent(inject).strip(),
                        'sys.argv = [%r, *%r]' % (str(ENTRY), list(argv)),
                        'runpy.run_path(%r, run_name="__main__")' % str(ENTRY)])
    # PYTHONPATH lets the injection import the checkout before entry/run.py does.
    child_env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT),
                 **(env or {})}
    child_env.pop('QMD_SANDBOX', None)
    result = subprocess.run([sys.executable, '-B', '-c', script],
                            input=json.dumps(payload).encode('utf-8') if payload is not None else b'',
                            capture_output=True, timeout=120, env=child_env, cwd=str(cwd or ROOT))
    return result.returncode, result.stdout.decode('utf-8'), result.stderr.decode('utf-8')


class SelectedHookServe(HostInstallCase):
    def recall_payload(self, session):
        return {'session_id': session, 'cwd': str(self.project), 'hook_event_name': 'UserPromptSubmit',
                'prompt': PROMPT}

    def hook_serve(self, payload, action='recall', **kwargs):
        return run_entry(['hook-serve', action, 'claude', str(self.project)], payload=payload,
                         cwd=self.project, **kwargs)

    def test_hook_serve_answers_a_selected_recall_with_exit_zero(self):
        code, out, err = self.hook_serve(self.recall_payload('entry-ok'))
        self.assertEqual(code, 0, err)
        self.assertNotIn('request refused', err)
        self.assertIn('zqheronx', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_an_exception_in_the_selected_serve_is_a_terse_exit_three(self):
        for action, payload in (
                ('recall', self.recall_payload('entry-boom')),
                ('compile', {'cwd': str(self.project), 'hook_event_name': 'PostToolUse',
                             'tool_name': 'Write', 'tool_input': {'file_path': 'notes/heron.md'}})):
            with self.subTest(action=action):
                code, out, err = self.hook_serve(payload, action, inject='''
                    import memvane.host.host_response as host
                    mock.patch.object(host, 'serve_selected',
                                      side_effect=PermissionError('/secret/path')).start()
                ''')
                self.assertEqual((code, out, err), (3, '', REFUSED))

    def test_a_forged_reader_record_refuses_recall_with_exit_three(self):
        from memvane.journal import api as journal
        record = dict(journal.inspect_journal(self.install.journal)['reader'])
        forged = {'head_floor': dict(record['head_floor'], epoch=record['head_floor']['epoch'] + 50)}
        if record.get('vector_profile'):
            forged['vector_profile'] = '0' * 64
        for key, value in forged.items():
            with self.subTest(field=key):
                changed = dict(record, **{key: value})
                with closing(sqlite3.connect(str(self.install.journal), isolation_level=None)) as db:
                    db.execute('UPDATE reader SET record=? WHERE id=1',
                               (json.dumps(changed, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=False, allow_nan=False),))
                code, out, err = self.hook_serve(self.recall_payload('entry-forged-' + key))
                self.assertEqual((code, out), (3, ''), err)
                self.assertRegex(err, r'^\[memvane\] [a-z0-9_]+; request refused\n$')

    def test_an_exhausted_hook_budget_never_exits_one(self):
        # The budget is spent before recall starts (a slow route selection):
        # hook_budget.remaining() raises HookDeadlineExceeded, a BaseException:
        # no context, exit 0.
        # A tiny inherited deadline is already past in any child: macOS system
        # Python 3.9 starts time.monotonic() near zero in every process.
        code, out, err = self.hook_serve(self.recall_payload('entry-deadline'),
                                         env={'QMD_HOOK_DEADLINE_MONO': '0.000001'})
        self.assertEqual((code, out), (0, ''), err)
        self.assertIn('hook_budget_exhausted', err)


class UnselectedEntries(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'plain'
        (self.project / '.memvane').mkdir(parents=True)
        (self.project / '.memvane' / 'settings.json').write_text(
            json.dumps({'name': 'plain', 'skipPaths': []}), encoding='utf-8')
        self.payload = self.tmp / 'event.json'
        self.payload.write_text(json.dumps({'cwd': str(self.project), 'hook_event_name': 'SessionStart',
                                            'session_id': 's-1'}), encoding='utf-8')

    def test_hook_classify_prints_the_route_state(self):
        code, out, err = run_entry(['hook-classify', 'recall', str(self.payload), str(self.project)])
        self.assertEqual((code, out, err), (0, 'legacy\n', ''))

    def test_an_exception_in_route_selection_never_exits_one(self):
        code, out, err = run_entry(['hook-classify', 'recall', str(self.payload), str(self.project)],
                                   inject='''
            import memvane.journal.api as journal
            mock.patch.object(journal, 'select_route', side_effect=LookupError('x')).start()
        ''')
        # run-hook refuses anything but a known state, so the fault answers legacy.
        self.assertEqual((code, out), (0, 'legacy\n'), err)
        self.assertIn('route_lookup_failed', err)

    def test_onboarding_notice_names_onboarding_for_a_project_with_settings(self):
        code, out, err = run_entry(['onboarding-notice', str(self.payload), str(self.project)])
        self.assertEqual((code, err), (0, ''))
        self.assertIn('setup skill로 온보딩', out)

    def test_an_exception_reading_settings_never_exits_one(self):
        code, out, err = run_entry(['onboarding-notice', str(self.payload), str(self.project)], inject='''
            import memvane.settings.api as settings
            mock.patch.object(settings, 'find_project_config',
                              side_effect=PermissionError('sandboxed')).start()
        ''')
        self.assertEqual((code, out), (0, ''), err)

    def test_run_hook_turns_entry_failures_into_silence_or_a_refusal(self):
        # Through the dispatcher an unselected project stays silent for every
        # action; SessionStart prints only the onboarding notice.
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS'):
            env.pop(key, None)
        for action in ('recall', 'gate', 'posttool', 'index', 'compile', 'update-queued'):
            with self.subTest(action=action):
                result = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), action, 'claude'],
                                        input=self.payload.read_text(), capture_output=True,
                                        text=True, timeout=60, env=env, cwd=str(ROOT))
                self.assertEqual((result.returncode, result.stderr), (0, ''))
                if action == 'update-queued':
                    self.assertIn('setup skill로 온보딩', result.stdout)
                else:
                    self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
