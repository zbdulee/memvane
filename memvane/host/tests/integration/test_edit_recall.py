"""Edit-time recall: the failure-silent tail of the selected ``posttool`` action.

Design r3 A and review B-1/B-2: the edit proof finishes first, then a recall
with the edit's own text injects at most two cards new to the session as
PostToolUse ``additionalContext`` (Claude, Codex). The tail never refuses,
never records a sync wake and keeps per-session state in the install state root.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest import mock

from memvane.host import edit_recall
from memvane.host import host_response as response
from memvane.host.tests.support import CARD_BODY, PROMPT, HostInstallCase, Wakes, context
from tests.support.install_builder import ROOT

EDIT_TEXT = ('Field notes for the pond survey. The emerald heron keeps the marker zqheronx '
             'beside the north pond, and the survey records each visit of the emerald heron '
             'with the date and the marker it carried. The marker zqheronx stays the same '
             'across every visit recorded in this survey.\n')
NEW_FILE = 'notes/pond.md'


def edit_payload(session, tool='Write', rel=NEW_FILE, text=EDIT_TEXT, **extra):
    if tool == 'Write':
        details = {'file_path': rel, 'content': text}
    elif tool == 'Edit':
        details = {'file_path': rel, 'old_string': 'x', 'new_string': text}
    elif tool == 'MultiEdit':
        half = len(text) // 2
        details = {'file_path': rel, 'edits': [{'old_string': 'a', 'new_string': text[:half]},
                                               {'old_string': 'b', 'new_string': text[half:]}]}
    elif tool == 'NotebookEdit':
        details = {'notebook_path': rel, 'new_source': text, 'cell_id': 'c1'}
    else:
        lines = ''.join('+' + line + '\n' for line in text.splitlines())
        details = {'command': '*** Begin Patch\n*** Add File: ' + rel + '\n' + lines + '*** End Patch'}
    return {'hook_event_name': 'PostToolUse', 'session_id': session, 'tool_name': tool,
            'tool_input': details, 'tool_use_id': 'tool-' + session, **extra}


class EditRecall(HostInstallCase):
    def setUp(self):
        super().setUp()
        self.wakes = Wakes().install(self)
        self.fixture.write_note(NEW_FILE, EDIT_TEXT)

    def edit_events(self, session=None):
        return [row for row in self.install.events() if row.get('event') == 'recall.edit'
                and (session is None or row.get('session') == session)]

    def posttool(self, payload, host='claude', cwd=None):
        with response.learning_capture() as slot:
            answer = self.serve('posttool', payload, host, cwd=cwd)
        return answer, list(slot)

    def test_each_edit_tool_injects_for_both_hosts(self):
        for host in ('claude', 'codex'):
            for tool in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'apply_patch'):
                if host == 'codex' and tool in ('MultiEdit', 'NotebookEdit'):
                    continue
                session = f'{host}-{tool}'
                with self.subTest(host=host, tool=tool):
                    answer, capture = self.posttool(edit_payload(session, tool), host)
                    self.assertEqual(answer['hookSpecificOutput']['hookEventName'], 'PostToolUse')
                    self.assertIn('zqheronx', context(answer))
                    self.assertIn('edit recall', context(answer))
                    [row] = self.edit_events(session)
                    self.assertEqual((row['outcome'], row['selection']), ('served', 'default_rank'))
                    self.assertEqual(row['cards'], row['selected'])
                    self.assertEqual(row['pool'], 1)
                    self.assertEqual(len(capture), 1)
                    entry = capture[0]
                    self.assertEqual(entry[2]['_memvane_request_id'], row['request_id'])
                    self.assertEqual(entry[2]['edit_event']['injected_ids'],
                                     [card['path'] for card in row['cards']])
                    self.assertEqual([item['path'] for item in entry[5]],
                                     [card['path'] for card in row['selected']])
        self.assertEqual(self.wakes.calls, [])

    def test_same_session_never_reinjects_a_card(self):
        first, _ = self.posttool(edit_payload('again'))
        self.assertIn('zqheronx', context(first))
        with mock.patch.object(edit_recall, 'throttled', return_value=False):
            second, _ = self.posttool(edit_payload('again'))
        self.assertEqual(second, {})
        rows = self.edit_events('again')
        self.assertEqual([row['outcome'] for row in rows], ['served', 'empty'])
        self.assertEqual(rows[1]['repeated'], 1)

    def test_a_card_from_prompt_recall_is_not_injected_again(self):
        code, out, err = self.main('recall', {'prompt': PROMPT, 'session_id': 'prompt-first',
                                              'hook_event_name': 'UserPromptSubmit'})
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', out)
        answer, _ = self.posttool(edit_payload('prompt-first'))
        self.assertEqual(answer, {})
        [row] = self.edit_events('prompt-first')
        self.assertEqual((row['outcome'], row['repeated']), ('empty', 1))

    def test_throttle_skips_a_second_edit_within_a_minute(self):
        first, _ = self.posttool(edit_payload('throttle'))
        self.assertIn('zqheronx', context(first))
        second, capture = self.posttool(edit_payload('throttle', rel='notes/other.md'))
        self.assertEqual((second, capture), ({}, []))
        self.assertEqual(len(self.edit_events('throttle')), 1)
        lines = [row for row in self.install.events()
                 if row.get('session') == 'throttle' and row.get('event') == 'posttool']
        self.assertEqual(lines[-1].get('editRecall'), 'skip_throttled')

    def test_a_held_session_lock_skips_the_recall(self):
        from memvane.diagnostics.api import state_root_for_decision
        state = state_root_for_decision(self.decision) / edit_recall.SESSION_DIR
        state.mkdir(mode=0o700, exist_ok=True)
        path = state / (hashlib.sha256(b'held').hexdigest() + '.json')
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            answer, capture = self.posttool(edit_payload('held'))
        finally:
            os.close(fd)
        self.assertEqual((answer, capture), ({}, []))
        self.assertEqual(self.edit_events('held'), [])

    def test_session_state_is_private_and_keeps_no_text(self):
        self.posttool(edit_payload('private'))
        from memvane.diagnostics.api import state_root_for_decision
        state = state_root_for_decision(self.decision) / edit_recall.SESSION_DIR
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        [path] = state.iterdir()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.name, hashlib.sha256(b'private').hexdigest() + '.json')
        stored = path.read_text()
        self.assertNotIn('zqheronx', stored)
        self.assertNotIn(NEW_FILE, stored)

    def test_skip_rules_run_no_recall(self):
        cases = (('.hidden/notes.md', EDIT_TEXT), ('node_modules/pkg/readme.md', EDIT_TEXT),
                 ('.memvane/wiki/new.md', EDIT_TEXT), ('notes/short.md', 'too short'),
                 ('notes/yarn.lock', EDIT_TEXT), ('notes/app.min.js', EDIT_TEXT),
                 ('notes/app.js.map', EDIT_TEXT), ('notes/image.png', EDIT_TEXT),
                 ('../outside.md', EDIT_TEXT))
        for index, (rel, text) in enumerate(cases):
            with self.subTest(rel=rel):
                session = f'skip-{index}'
                answer, capture = self.posttool(edit_payload(session, rel=rel, text=text))
                self.assertEqual((answer, capture), ({}, []))
                self.assertEqual(self.edit_events(session), [])

    def test_skip_paths_and_switch_and_failed_edit_and_hermes(self):
        with mock.patch.dict(os.environ, {'MEMVANE_EDIT_RECALL': 'off'}):
            self.assertEqual(self.posttool(edit_payload('off'))[0], {})
        failed = edit_payload('failed', tool_response={'success': False})
        self.assertEqual(self.posttool(failed)[0], {})
        self.assertEqual(self.posttool(edit_payload('hermes'), 'hermes')[0], {})
        self.assertEqual(self.posttool({**edit_payload('x'), 'session_id': None})[0], {})
        settings = dict(self.decision['recallSettings'], skipPaths=['notes'])
        with mock.patch.dict(self.decision, recallSettings=settings):
            with self.assertRaises(edit_recall.Skip):
                edit_recall.edit_query(self.decision, edit_payload('s'), self.project, [NEW_FILE])
        self.assertEqual(self.edit_events(), [])

    def test_a_self_citing_card_is_not_injected_but_stays_in_the_pool(self):
        payload = edit_payload('self', rel='notes/heron.md')
        answer, capture = self.posttool(payload)
        self.assertEqual(answer, {})
        [row] = self.edit_events('self')
        self.assertEqual((row['selfCited'], row['pool'], row['cards']), (1, 1, []))
        self.assertEqual(len(capture[0][4]), 1)

    def test_selector_choice_injects_up_to_two_cards(self):
        def select(pool, **kwargs):
            seen.update(kwargs)
            return {'reason': 'selected', 'final_results': list(pool), 'resources': {
                'pairs': 1, 'batches': 1, 'forward_seconds': 0.25, 'device': 'mps:0',
                'planned_pairs': 1}}
        seen = {}
        with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                mock.patch('memvane.learning.api.select_published', side_effect=select):
            answer, _ = self.posttool(edit_payload('selector'))
        self.assertIn('zqheronx', context(answer))
        self.assertTrue(seen['skip_busy'])
        self.assertLessEqual(len(seen['prompt']), 2000)
        [row] = self.edit_events('selector')
        self.assertEqual((row['selection'], row['selectorReason']), ('selector', 'selected'))
        self.assertEqual((row['selectorPairs'], row['selectorBatches'], row['selectorDevice']),
                         (1, 1, 'mps'))
        self.assertEqual(row['selectorBatchMs'], 250.0)

    def test_selector_past_its_deadline_falls_back_to_one_ranked_card(self):
        def stalled(pool, **kwargs):
            # The selector gives up at its own deadline (live_select's timeout).
            self.assertLess(kwargs['deadline'], time.monotonic() + edit_recall.EDIT_RECALL_SECONDS)
            return {'reason': 'timeout', 'final_results': []}
        with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                mock.patch('memvane.learning.api.select_published', side_effect=stalled):
            answer, capture = self.posttool(edit_payload('stall'))
        self.assertIn('zqheronx', context(answer))
        [row] = self.edit_events('stall')
        self.assertEqual((row['selection'], row['selectorReason']), ('default_rank', 'ranked'))
        self.assertEqual(len(capture), 1)

    def test_search_past_the_budget_injects_nothing_and_captures_nothing(self):
        from memvane.recall import api as recall_api
        original = recall_api.query_endpoint

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            time.sleep(0.8)  # the search answers after the tail's budget
            return result
        from memvane.kernel import hook_budget
        with mock.patch.object(edit_recall, 'EDIT_RECALL_SECONDS', 0.6), \
                mock.patch.object(edit_recall, 'SELECTOR_RESERVE_SECONDS', 0.1), \
                mock.patch.object(hook_budget, '_deadline', time.monotonic() + 18), \
                mock.patch.object(recall_api, 'query_endpoint', side_effect=slow):
            edit_recall.mark_hook_started(time.monotonic())
            try:
                answer, capture = self.posttool(edit_payload('budget'))
            finally:
                edit_recall.mark_hook_started(None)
        self.assertEqual((answer, capture), ({}, []))
        [row] = self.edit_events('budget')
        self.assertIn(row['outcome'], ('budget_exceeded', 'refused'))

    def test_a_proof_change_inside_the_tail_answers_empty(self):
        from memvane.recall.types import HostResponseError
        with mock.patch('memvane.recall.api.serve_endpoint_recall',
                        side_effect=HostResponseError('selected_proof_changed')):
            answer, capture = self.posttool(edit_payload('moved'))
        self.assertEqual((answer, capture), ({}, []))
        [row] = self.edit_events('moved')
        self.assertEqual((row['outcome'], row['reason']), ('refused', 'selected_proof_changed'))

    def test_event_and_capture_keep_no_edited_path(self):
        answer, capture = self.posttool(edit_payload('nopath'))
        self.assertIn('zqheronx', context(answer))
        [row] = self.edit_events('nopath')
        dumped = json.dumps(row)
        self.assertNotIn(NEW_FILE, dumped)
        self.assertNotIn('Field notes', dumped)
        event = capture[0][2]['edit_event']
        self.assertNotIn(NEW_FILE, json.dumps(event))
        self.assertEqual(event['path_sha256'], hashlib.sha256(NEW_FILE.encode()).hexdigest())
        self.assertEqual(capture[0][2]['prompt'], EDIT_TEXT)

    def test_the_captured_case_joins_the_event_by_request_id(self):
        # A superset of the first card's words: the same search finds both.
        # Recall serves only cards with compiler provenance (``sourceRevisions``).
        self.fixture.write_card('pond.md', title='North pond', sources=[self.project / 'notes/heron.md'],
                                body=CARD_BODY.strip() + ' The survey copy.\n')
        self.fixture.commit('second card')
        self.install.settle()
        answer, capture = self.posttool(edit_payload('joined'))
        self.assertIn('zqheronx', context(answer))
        [row] = self.edit_events('joined')
        host, decision, payload, settings, pool, selected = capture[0]
        self.assertEqual(len(pool), 2)
        state = self.tmp / 'learning-state'
        state.mkdir(mode=0o700)
        settings = dict(settings, contextLearning={**settings['contextLearning'],
                                                   'stateRoot': str(state)})
        from memvane.learning.api import capture_recall
        self.assertEqual(capture_recall(host, decision, payload, settings, pool, selected), 'stored')
        [database] = state.rglob('learning.sqlite3')
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(database))) as db:
            [(raw,)] = db.execute('SELECT body FROM samples').fetchall()
        sample = json.loads(raw)
        self.assertEqual((sample['schema_version'], sample['request_id']), (5, row['request_id']))
        self.assertEqual(sample['edit_event']['injected_ids'], [card['path'] for card in row['cards']])
        self.assertEqual(sample['sampling']['baseline_selected_ids'],
                         [card['path'] for card in row['selected']])
        self.assertEqual(sample['prompt']['text'], EDIT_TEXT)
        self.assertNotIn(NEW_FILE, raw)
        self.assertNotIn('sources:', raw)

    def test_a_project_without_learning_still_gets_edit_recall(self):
        # The fixture install has no learning enrollment: injection only.
        answer, capture = self.posttool(edit_payload('unenrolled'))
        self.assertIn('zqheronx', context(answer))
        host, decision, payload, settings, pool, selected = capture[0]
        off = dict(settings, contextLearning={**settings['contextLearning'], 'capture': False})
        from memvane.learning.api import capture_recall
        self.assertEqual(capture_recall(host, decision, payload, off, pool, selected), 'disabled')

    def test_linked_worktree_injects_from_its_own_view_without_capture(self):
        linked = self.fixture.linked_worktree('wt', 'wt-branch')
        linked.write_note(NEW_FILE, EDIT_TEXT)
        self.install.settle()
        answer, capture = self.posttool(edit_payload('linked'), cwd=linked.root)
        self.assertEqual(capture, [])
        rows = self.edit_events('linked')
        self.assertEqual(len(rows), 1)
        if rows[0]['outcome'] == 'served':
            self.assertIn('zqheronx', context(answer))
        else:
            self.assertEqual(answer, {})
        self.assertEqual(self.wakes.calls, [])


class EditRecallEntry(HostInstallCase):
    """The real dispatcher: ``hooks/run-hook posttool claude|codex`` in a child."""

    def setUp(self):
        super().setUp()
        self.fixture.write_note(NEW_FILE, EDIT_TEXT)

    def run_hook(self, payload, host='claude', env=None):
        child_env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', **(env or {})}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'CLAUDE_PLUGIN_ROOT',
                    'PLUGIN_ROOT'):
            child_env.pop(key, None)
        return subprocess.Popen(['bash', str(ROOT / 'hooks' / 'run-hook'), 'posttool', host],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=child_env, cwd=str(self.project))

    def finish(self, child, payload):
        out, err = child.communicate(json.dumps({'cwd': str(self.project), **payload}).encode(),
                                     timeout=120)
        return child.returncode, out.decode(), err.decode()

    def test_run_hook_posttool_injects_for_claude_and_codex(self):
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                payload = edit_payload('entry-' + host, 'Write' if host == 'claude' else 'apply_patch')
                code, out, err = self.finish(self.run_hook(payload, host), payload)
                self.assertEqual(code, 0, err)
                answer = json.loads(out)
                self.assertEqual(answer['hookSpecificOutput']['hookEventName'], 'PostToolUse')
                self.assertIn('zqheronx', answer['hookSpecificOutput']['additionalContext'])

    def test_parallel_posttool_runs_one_recall(self):
        payload = edit_payload('parallel')
        children = [self.run_hook(payload) for _ in range(2)]
        results = [self.finish(child, payload) for child in children]
        self.assertEqual([code for code, _out, _err in results], [0, 0])
        rows = [row for row in self.install.events()
                if row.get('event') == 'recall.edit' and row.get('session') == 'parallel']
        self.assertEqual(len(rows), 1)
        injected = [out for _code, out, _err in results if 'zqheronx' in out]
        self.assertLessEqual(len(injected), 1)

    def test_a_tail_failure_is_exit_zero_with_an_empty_answer(self):
        from tests.support.install_builder import ROOT as root
        script = ('import runpy, sys\nfrom unittest import mock\n'
                  'from memvane.recall.types import HostResponseError\n'
                  "mock.patch('memvane.recall.api.serve_endpoint_recall', "
                  "side_effect=HostResponseError('selected_proof_changed')).start()\n"
                  'sys.argv = [%r, "hook-serve", "posttool", "codex", %r]\n'
                  'runpy.run_path(%r, run_name="__main__")\n'
                  % (str(root / 'entry' / 'run.py'), str(self.project), str(root / 'entry' / 'run.py')))
        payload = {'cwd': str(self.project), **edit_payload('tail-fail', 'apply_patch')}
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root)}
        env.pop('QMD_SANDBOX', None)
        done = subprocess.run([__import__('sys').executable, '-B', '-c', script],
                              input=json.dumps(payload).encode(), capture_output=True,
                              timeout=120, env=env, cwd=str(self.project))
        self.assertEqual((done.returncode, done.stdout.decode()), (0, '{}\n'), done.stderr.decode())


if __name__ == '__main__':
    unittest.main()
