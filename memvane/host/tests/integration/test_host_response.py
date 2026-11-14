"""Selected-route hook responses: SessionStart, gate, posttool, index, Bash, compile and recall.

``SelectedActions`` serves a real selected install (``build_install``); the
recorded durable wakes come from ``Wakes`` (``runtime.api.request``), because
the fixture switch makes the real request a no-op. ``RecallRetry`` and
``HookEntry`` drive the retry and the ``main`` entry over synthetic decisions
by replacing one attempt (``_serve_selected_once``) or the whole serve.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import threading
import time
import unittest
from unittest import mock

from memvane.host import host_response as response
from memvane.host.tests.support import PROMPT, HostInstallCase, Wakes, context
from memvane.journal.types import JournalEvidenceError
from memvane.recall.types import HostResponseError
from tests.support.case import FixtureCase

SESSION_NOTICE = '[memvane] selected source update queued; recall requires a fresh published view.'


class SelectedActions(HostInstallCase):
    def setUp(self):
        super().setUp()
        self.wakes = Wakes().install(self)

    def assert_sync_woken(self):
        """Durable wakes only, all for the main checkout, and no sync ran in the hook."""
        self.assertIn('laya-sync', self.wakes.kinds())
        self.assertLessEqual(set(self.wakes.kinds()), {'laya-sync', 'laya-compile'})
        self.assertEqual({target for _kind, target, _kwargs in self.wakes.calls}, {str(self.project)})

    def edit(self, rel, tool='Write', event='PostToolUse', **response_fields):
        payload = {'hook_event_name': event, 'tool_name': tool,
                   'tool_input': {'file_path': rel, 'content': 'x'}}
        if response_fields:
            payload['tool_response'] = response_fields
        return payload

    def test_recall_serves_the_published_card_for_every_host(self):
        for host in ('claude', 'codex', 'hermes'):
            with self.subTest(host=host):
                answer = self.serve('recall', {'prompt': PROMPT, 'session_id': 's-' + host,
                                               'hook_event_name': 'UserPromptSubmit'}, host)
                text = context(answer, host)
                self.assertIn('zqheronx', text)
                self.assertIn('heron.md', text)
                if host != 'hermes':
                    self.assertEqual(answer['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        # The fixture provider refuses query admission: lexical rows serve and
        # the only wake is the provider owner's own sync wake.
        self.assertLessEqual({(kind, target) for kind, target, _kwargs in self.wakes.calls},
                             {('laya-sync', str(self.project))})
        lines = [line for line in self.install.events() if line.get('event') == 'recall']
        self.assertEqual([line['outcome'] for line in lines], ['served'] * 3)
        self.assertTrue(all(line['cards'] and set(line['prompt']) == {'sha256', 'chars'}
                            for line in lines), lines)

    def test_session_start_records_sync_wakes_and_returns_the_queued_notice(self):
        epoch = self.install.epoch()
        for host, action in (('claude', 'update-queued'), ('codex', 'update'),
                             ('hermes', 'update-queued')):
            with self.subTest(host=host, action=action):
                self.wakes.calls.clear()
                answer = self.serve(action, {'hook_event_name': 'SessionStart'}, host)
                self.assert_sync_woken()
                self.assertEqual(self.install.epoch(), epoch)
                if host == 'hermes':
                    self.assertEqual(answer, {'status': 'queued'})
                else:
                    self.assertEqual(answer['hookSpecificOutput'],
                                     {'hookEventName': 'SessionStart',
                                      'additionalContext': SESSION_NOTICE})

    def test_session_start_needs_a_session_event(self):
        with self.assertRaisesRegex(HostResponseError, '^invalid_session_event$'):
            self.serve('update-queued', {'hook_event_name': 'UserPromptSubmit'})
        self.assertEqual(self.wakes.calls, [])

    def test_an_unrecorded_wake_refuses_session_start(self):
        for status in ('dropped', None):
            with self.subTest(status=status):
                self.wakes.status = status
                with self.assertRaisesRegex(HostResponseError, '^selected_sync_enqueue_failed$'):
                    self.serve('update-queued', {'hook_event_name': 'SessionStart'})
        self.wakes.status = 'deferred_queue_capacity_retained'
        self.serve('update-queued', {'hook_event_name': 'SessionStart'})

    def test_session_start_wakes_compile_for_a_pending_refresh(self):
        # The first SessionStart also records the coalesced lifecycle request.
        self.serve('update', {'hook_event_name': 'SessionStart'}, 'codex')
        self.wakes.calls.clear()
        self.serve('update', {'hook_event_name': 'SessionStart'}, 'codex')
        self.assertNotIn('laya-compile', self.wakes.kinds())
        self.wakes.calls.clear()
        with mock.patch('memvane.wiki.card.api.unresolved_pending_refreshes_strict',
                        return_value=[{'sourcePath': 'notes/heron.md'}]):
            self.serve('update', {'hook_event_name': 'SessionStart'}, 'codex')
        self.assertIn('laya-compile', self.wakes.kinds())
        self.assert_sync_woken()
        self.wakes.calls.clear()
        with mock.patch('memvane.wiki.card.api.unresolved_pending_refreshes_strict', return_value=None):
            with self.assertRaisesRegex(HostResponseError, '^wiki_pending_refresh_unknown$'):
                self.serve('update', {'hook_event_name': 'SessionStart'}, 'codex')

    def test_a_successful_bash_wakes_the_full_scan_owner_only(self):
        bash = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                'tool_input': {'command': 'git switch qa/feature'}}
        cases = {'ok': ({'exit_code': 0}, True),
                 'official': ({'stdout': '', 'stderr': '', 'interrupted': False}, True),
                 'failed': ({'exit_code': 1}, False),
                 'interrupted': ({'interrupted': True, 'stdout': '', 'stderr': ''}, False),
                 'background': ({'exit_code': 0, 'backgroundTaskId': 'b1'}, False)}
        for name, (tool_response, woken) in cases.items():
            with self.subTest(case=name):
                self.wakes.calls.clear()
                self.assertEqual(self.serve('file-change-event',
                                            {**bash, 'tool_response': tool_response}), {})
                if woken:
                    self.assert_sync_woken()
                else:
                    self.assertEqual(self.wakes.calls, [])

    def test_index_wakes_sync_only_for_a_successful_edit(self):
        self.assertEqual(self.serve('index', self.edit('notes/heron.md')), {})
        self.assert_sync_woken()
        self.wakes.calls.clear()
        self.assertEqual(self.serve('index', self.edit('notes/heron.md', is_error=True)), {})
        self.assertEqual(self.wakes.calls, [])

    def test_gate_and_posttool_accept_project_and_external_edits(self):
        for action, event in (('gate', 'PreToolUse'), ('posttool', 'PostToolUse')):
            for rel in ('notes/heron.md', '../outside.md'):
                with self.subTest(action=action, rel=rel):
                    self.assertEqual(self.serve(action, self.edit(rel, event=event)), {})
        self.assertEqual(self.serve('gate', self.edit('a.md', event='PreToolUse'), 'hermes'),
                         {'action': 'continue'})
        self.assertEqual(self.wakes.calls, [])

    def test_gate_refuses_an_edit_event_outside_the_project(self):
        from memvane.host.api import serve_selected
        event = {**self.edit('notes/heron.md', event='PreToolUse'), 'cwd': str(self.project.parent)}
        with self.assertRaisesRegex(HostResponseError, '^edit_outside_project$'):
            serve_selected('gate', 'claude', self.select('gate'), event)

    def test_posttool_needs_a_real_patch_target(self):
        patch = {'hook_event_name': 'PostToolUse', 'tool_name': 'apply_patch',
                 'tool_input': {'patch': '*** Begin Patch\n*** Update File: notes/heron.md\n*** End Patch'}}
        self.assertEqual(self.serve('posttool', patch), {})
        empty = {**patch, 'tool_input': {'patch': '*** Begin Patch\n*** End Patch'}}
        with self.assertRaisesRegex(HostResponseError, '^edit_path_missing$'):
            self.serve('posttool', empty)

    def test_compile_hands_the_proved_edit_to_the_source_queue(self):
        event = self.edit('notes/heron.md')
        with mock.patch('memvane.wiki.intake.api.enqueue_payload',
                        return_value={'queued': 1, 'reason': 'queued'}) as enqueue:
            self.assertEqual(self.serve('compile', event, 'codex'), {})
        enqueue.assert_called_once_with({'cwd': str(self.project), **event, 'engine': 'codex'})
        with mock.patch('memvane.wiki.intake.api.enqueue_payload', return_value={'queued': -1}):
            with self.assertRaisesRegex(HostResponseError, '^selected_compile_enqueue_failed$'):
                self.serve('compile', event, 'codex')
        with self.assertRaisesRegex(HostResponseError, '^invalid_edit_event$'):
            self.serve('compile', self.edit('notes/heron.md', event='PreToolUse'))

    def test_capture_slot_is_per_call_and_absent_for_in_process_hosts(self):
        payload = {'prompt': PROMPT, 'hook_event_name': 'UserPromptSubmit'}
        self.serve('recall', payload, 'hermes')
        self.assertIsNone(response._LEARNING_CAPTURE.get())
        with response.learning_capture() as outer:
            self.serve('recall', payload, 'codex')
            with response.learning_capture() as inner:
                self.serve('recall', payload, 'claude')
            self.assertIs(response._LEARNING_CAPTURE.get(), outer)
        self.assertIsNone(response._LEARNING_CAPTURE.get())
        self.assertEqual([entry[0] for entry in outer], ['codex'])
        self.assertEqual([entry[0] for entry in inner], ['claude'])

    def test_main_serves_recall_and_logs_one_event_line(self):
        code, out, err = self.main('recall', {'prompt': PROMPT, 'session_id': 'main-1'})
        self.assertEqual(code, 0, err)
        self.assertNotIn('request refused', err)
        self.assertIn('zqheronx', json.loads(out)['hookSpecificOutput']['additionalContext'])
        lines = [line for line in self.install.events() if line.get('session') == 'main-1']
        self.assertEqual([(line['event'], line['outcome']) for line in lines], [('recall', 'served')])

    def test_main_refuses_a_route_that_changed_under_the_hook_with_one_line(self):
        moved = dict(self.decision, scopeId='0' * 32)
        with mock.patch.object(response, '_reselect', return_value=moved):
            code, out, err = self.main('recall', {'prompt': PROMPT, 'session_id': 'main-2'})
        self.assertEqual((code, out), (3, ''))
        self.assertEqual(err, '[memvane] selected_route_changed; request refused\n')
        lines = [line for line in self.install.events() if line.get('session') == 'main-2']
        self.assertEqual([(line['outcome'], line['reason']) for line in lines],
                         [('refused', 'selected_route_changed')])


def proof(epoch=3, **changes):
    return {'productRouteAllowed': True, 'selectionEpoch': epoch, 'stateFormat': 1,
            'writerFloor': '1.0.0', **changes}


class RecallRetry(unittest.TestCase):
    """One proof or view change inside a recall reselects once; anything else refuses."""

    DECISION = {'state': 'candidate', 'projectRoot': '/synthetic/project', 'installId': 'c' * 32,
                'scopeId': '6' * 32, 'journalPath': '/synthetic/state/journal.sqlite',
                'storePath': '/synthetic/state/store.sqlite', 'storeIncarnation': 'a' * 32}
    SERVED = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'ok'}}

    def setUp(self):
        from memvane.kernel import hook_budget
        patcher = mock.patch.object(hook_budget, '_deadline', None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logged = []
        log = mock.patch.object(response, '_write_selection_log',
                                side_effect=lambda *args, **kwargs: self.logged.append((args, kwargs)))
        log.start()
        self.addCleanup(log.stop)
        emit = mock.patch('memvane.diagnostics.api.emit')
        emit.start()
        self.addCleanup(emit.stop)

    def recall(self, attempts, reselect=None):
        """``attempts``: per attempt, an exception to raise or an answer to return."""
        seen = []

        def once(_action, _host, decision, _payload):
            seen.append(decision)
            step = attempts[len(seen) - 1]
            if isinstance(step, BaseException):
                raise step
            return step
        with mock.patch.object(response, '_serve_selected_once', side_effect=once), \
                mock.patch.object(response, 'select_route',
                                  side_effect=reselect or (lambda *_a, **_k: dict(self.DECISION))) as select:
            try:
                result = response.serve_selected('recall', 'codex', dict(self.DECISION),
                                                 {'prompt': 'find alpha', 'cwd': '/synthetic/project'})
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                result = exc
        return result, seen, select

    def test_a_change_of_the_same_install_retries_once_from_a_fresh_route(self):
        for exc in (HostResponseError('selected_proof_changed'),
                    HostResponseError('incremental_base_changed'),
                    JournalEvidenceError('host_route_unverified'),
                    JournalEvidenceError('host_decision_changed'),
                    JournalEvidenceError('reader_selection_changed'),
                    ValueError('reader_store_changed')):
            with self.subTest(code=str(exc)):
                self.logged.clear()
                result, seen, select = self.recall([exc, self.SERVED])
                self.assertEqual(result, self.SERVED)
                self.assertEqual(len(seen), 2)
                self.assertEqual(select.call_args, mock.call('/synthetic/project', action='recall'))
                self.assertEqual([(args[1], kwargs) for args, kwargs in self.logged],
                                 [('laya_recall_proof_retry', {'retry': str(exc)})])

    def test_a_second_change_still_refuses(self):
        first, second = HostResponseError('selected_proof_changed'), HostResponseError('selected_proof_changed')
        result, seen, select = self.recall([first, second])
        self.assertIs(result, second)
        self.assertEqual((len(seen), select.call_count), (2, 1))

    def test_a_changed_identity_keeps_the_refusal(self):
        for key, value in (('installId', 'f' * 32), ('projectRoot', '/synthetic/other'),
                           ('scopeId', '0' * 32), ('journalPath', '/synthetic/x/journal.sqlite'),
                           ('storePath', '/synthetic/x/store.sqlite'),
                           ('storeIncarnation', 'b' * 32), ('state', 'blocked')):
            with self.subTest(identity=key):
                moved = dict(self.DECISION, **{key: value})
                exc = HostResponseError('selected_proof_changed')
                result, seen, select = self.recall([exc, self.SERVED],
                                                   reselect=lambda *_a, **_k: dict(moved))
                self.assertIs(result, exc)
                self.assertEqual((len(seen), select.call_count), (1, 1))

    def test_other_refusals_are_never_retried(self):
        reader = HostResponseError('selected_proof_changed')
        reader._memvane_reader_attempted = True
        for exc in (HostResponseError('index_stale_sync_queued'),
                    HostResponseError('selected_proof_rolled_back'),
                    JournalEvidenceError('inactive_or_mismatched_journal'),
                    ValueError('reader_endpoint_changed'), reader):
            with self.subTest(code=str(exc)):
                result, seen, select = self.recall([exc, self.SERVED])
                self.assertIs(result, exc)
                self.assertEqual(len(seen), 1)
                select.assert_not_called()

    def test_the_retry_needs_the_armed_hook_budget(self):
        from memvane.kernel import hook_budget
        with self.subTest('budget nearly spent'):
            hook_budget._deadline = time.monotonic() + 0.5
            result, seen, select = self.recall([HostResponseError('selected_proof_changed'), self.SERVED])
            self.assertEqual((str(result), len(seen)), ('selected_proof_changed', 1))
            select.assert_not_called()
        with self.subTest('reselection spends the budget'):
            hook_budget._deadline = time.monotonic() + 10

            def slow(*_a, **_k):
                hook_budget._deadline = time.monotonic() + 0.5
                return dict(self.DECISION)
            result, seen, select = self.recall([HostResponseError('selected_proof_changed'), self.SERVED],
                                               reselect=slow)
            self.assertEqual((str(result), len(seen), select.call_count), ('selected_proof_changed', 1, 1))
        with self.subTest('enough budget'):
            hook_budget._deadline = time.monotonic() + 10
            result, seen, _select = self.recall([HostResponseError('selected_proof_changed'), self.SERVED])
            self.assertEqual((result, len(seen)), (self.SERVED, 2))

    def test_the_retry_drops_the_first_attempts_learning_capture(self):
        def first(*_args):
            response._set_learning_capture(('codex', 'first'))
            raise HostResponseError('selected_proof_changed')
        captured = []

        def second(*_args):
            captured.append(list(slot))
            return self.SERVED
        with response.learning_capture() as slot:
            attempts = iter((first, second))
            with mock.patch.object(response, '_serve_selected_once',
                                   side_effect=lambda *args: next(attempts)(*args)), \
                    mock.patch.object(response, 'select_route', return_value=dict(self.DECISION)):
                result = response.serve_selected('recall', 'codex', dict(self.DECISION),
                                                 {'prompt': 'find alpha', 'cwd': '/synthetic/project'})
        self.assertEqual(result, self.SERVED)
        self.assertEqual(captured, [[]])

    def test_overlapping_recalls_keep_their_own_floor_and_wakes(self):
        # Hermes serves recalls in process: a concurrent or nested recall must
        # neither reset another one's epoch floor nor share its wakes.
        from memvane.recall import api as recall
        a_ready, b_done = threading.Event(), threading.Event()
        outcome = {}

        def epoch(value):
            recall.recall_epoch({'selectionEpoch': value})

        def once(_action, _host, decision, _payload):
            root = decision['projectRoot']
            if root == '/p/a':
                epoch(5)
                epoch(6)
                recall.recall_sync_wake('/p/a')
                a_ready.set()
                b_done.wait(5)
                response.serve_selected('recall', 'hermes', {'projectRoot': '/p/n'}, {'prompt': 'q'})
                recall.recall_sync_wake('/p/a')
                recall.recall_sync_wake('/p/a2')
                try:
                    epoch(5)
                except HostResponseError as exc:
                    outcome['a'] = str(exc)
                return {}
            if root == '/p/b':
                a_ready.wait(5)
                epoch(1)
                recall.recall_sync_wake('/p/b')
                recall.recall_sync_wake('/p/b')
                return {}
            epoch(0)
            recall.recall_sync_wake(root)
            return {}

        def run(root):
            try:
                response.serve_selected('recall', 'claude', {'projectRoot': root}, {'prompt': 'q'})
            finally:
                if root == '/p/b':
                    b_done.set()
        wakes = Wakes()
        with mock.patch.object(response, '_serve_selected_once', side_effect=once), \
                mock.patch('memvane.runtime.api.request', side_effect=wakes):
            threads = [threading.Thread(target=run, args=(root,)) for root in ('/p/a', '/p/b')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            recall.recall_sync_wake('/p/free')
            recall.recall_sync_wake('/p/free')
        self.assertEqual(outcome.get('a'), 'selected_proof_rolled_back')
        self.assertEqual(sorted(target for _kind, target, _kwargs in wakes.calls),
                         ['/p/a', '/p/a2', '/p/b', '/p/free', '/p/free', '/p/n'])


class HookEntry(FixtureCase):
    """``host_response.main``: one JSON answer on stdout or a terse refusal with exit 3."""

    def setUp(self):
        super().setUp()
        from memvane.kernel import hook_budget
        budget = mock.patch.object(hook_budget, '_deadline', None)  # main arms the module budget
        budget.start()
        self.addCleanup(budget.stop)
        self.payload = self.tmp / 'payload.json'
        self.payload.write_text(json.dumps({'prompt': 'find alpha', 'cwd': str(self.tmp)}),
                                encoding='utf-8')
        self.selection = {'state': 'candidate', 'projectRoot': str(self.tmp)}

    def main(self, action, serve, **patches):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(response, 'select_hook_route', return_value=self.selection), \
                mock.patch.object(response, 'select_route', return_value=self.selection), \
                mock.patch.object(response, 'serve_selected', side_effect=serve), \
                mock.patch('memvane.diagnostics.api.emit'), \
                redirect_stdout(out), redirect_stderr(err):
            code = response.main(['serve-hook', action, 'claude', str(self.payload), str(self.tmp)])
        return code, out.getvalue(), err.getvalue()

    def test_refusals_exit_three_with_their_fixed_code_and_no_output(self):
        from memvane.kernel.errors import IndexContractError
        cases = ((HostResponseError('index_stale_sync_queued'), 'index_stale_sync_queued'),
                 (HostResponseError('selected_provider_preparing'), 'selected_provider_preparing'),
                 (IndexContractError('vector_deadline_exceeded'), 'vector_deadline_exceeded'),
                 (IndexContractError('reader_snapshot_unpinned'), 'reader_snapshot_unpinned'),
                 (IndexContractError('reader_selection_unproved'), 'reader_selection_unproved'),
                 (JournalEvidenceError('reader_store_recovery_pending'), 'reader_store_recovery_pending'),
                 (IndexContractError('reader_secret_prompt=synthetic-secret'),
                  'selected_proof_or_query_failed'),
                 (RuntimeError('boom /secret/path'), 'selected_proof_or_query_failed'),
                 (KeyError('projectRoot'), 'selected_proof_or_query_failed'))
        for raised, code in cases:
            with self.subTest(code=code):
                status, out, err = self.main('recall', raised)
                self.assertEqual((status, out), (3, ''))
                self.assertEqual(err, f'[memvane] {code}; request refused\n')

    def test_the_event_line_names_the_code_never_the_message(self):
        from memvane.kernel.errors import IndexContractError
        for error, reason in ((IndexContractError('reader_sidecar_revision_unproved'),
                               'reader_sidecar_revision_unproved'),
                              (IndexContractError('reader_secret_prompt=synthetic-secret'),
                               'selected_proof_or_query_failed')):
            with self.subTest(reason=reason):
                with mock.patch('memvane.diagnostics.api.emit') as emit:
                    with self.assertRaises(IndexContractError):
                        with response._event_scope('recall', 'claude', self.selection,
                                                   {'prompt': 'synthetic-secret'}):
                            raise error
                self.assertEqual(emit.call_args.kwargs['reason'], reason)
                self.assertNotIn('synthetic-secret', json.dumps(emit.call_args.kwargs, default=str))

    def test_an_unselected_route_is_refused_before_any_serve(self):
        self.selection = {'state': 'legacy'}
        status, out, err = self.main('recall', AssertionError('served'))
        self.assertEqual((status, out), (3, ''))
        self.assertEqual(err, '[memvane] selected_route_unavailable; request refused\n')

    def test_main_arms_the_hook_budget_before_route_selection(self):
        from memvane.kernel import hook_budget
        seen = {}

        def select(*_args):
            seen['select'] = hook_budget.deadline()
            return self.selection
        before = time.monotonic()
        with mock.patch.object(hook_budget, '_deadline', None), \
                mock.patch.object(response, 'select_hook_route', side_effect=select), \
                mock.patch.object(response, 'select_route', return_value=self.selection), \
                mock.patch.object(response, 'serve_selected', return_value={}), \
                mock.patch.object(response, '_capture_learning'), \
                mock.patch.dict(os.environ, {'QMD_HOOK_DEADLINE_MONO': ''}), \
                redirect_stdout(io.StringIO()):
            status = response.main(['serve-hook', 'recall', 'claude', str(self.payload), str(self.tmp)])
            exported = os.environ.get('QMD_HOOK_DEADLINE_MONO')
        self.assertEqual(status, 0)
        self.assertGreater(seen['select'], before)
        self.assertLessEqual(seen['select'], before + hook_budget.MAX_SECONDS)
        self.assertEqual(exported, '')

    def test_capture_runs_after_the_answer_is_flushed_and_never_changes_it(self):
        answer = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'ctx'}}
        events = []
        stdout = io.StringIO()

        def serve(*_args):
            response._set_learning_capture(('codex', self.selection, {}, {}, [], []))
            return answer

        def capture(*_args, **_kwargs):
            events.append(stdout.getvalue())
            raise RuntimeError('capture exploded')
        for action in ('recall', 'posttool'):
            with self.subTest(action=action):
                events.clear()
                stdout.seek(0)
                stdout.truncate()
                with mock.patch.object(response, 'select_hook_route', return_value=self.selection), \
                        mock.patch.object(response, 'select_route', return_value=self.selection), \
                        mock.patch.object(response, 'serve_selected', side_effect=serve), \
                        mock.patch('memvane.learning.api.capture_recall', side_effect=capture), \
                        redirect_stdout(stdout):
                    code = response.main(['serve-hook', action, 'codex', str(self.payload),
                                          str(self.tmp)])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue()), answer)
                self.assertEqual(len(events), 1 if action == 'recall' else 0)
                if events:
                    self.assertEqual(json.loads(events[0]), answer)
                self.assertIsNone(response._LEARNING_CAPTURE.get())


if __name__ == '__main__':
    unittest.main()
