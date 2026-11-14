"""Hermes registered hooks over real route states: selected, preview, interrupted and damaged.

The registered callbacks run the real selector child (``entry/run.py
hook-classify``/``classify``) and the in-process host api. A selected route
serves recall; a preview recalls nothing yet; an interrupted window is quiet;
a damaged or changed route is a visible refusal for recall and a block for the
edit gate, while SessionStart and post-edit hooks only warn.
"""
import logging
import os
import subprocess
import unittest
from unittest import mock

from hermes_adapter import core_bridge as bridge
from hermes_adapter import plugin
from memvane.host import api as host_api
from memvane.host.tests.support import NOTE, PROMPT, HostInstallCase, Wakes
from tests.support.case import FixtureCase
from tests.support.install_builder import ROOT, build_install
from tests.support.project import make_project

QUIET = {'QMD_SANDBOX': '', 'HERMES_SANDBOX': '', 'HERMES_HEADLESS': '', 'CODEX_SANDBOX': '',
         'CLAUDE_SANDBOX': '', 'GEMINI_SANDBOX': '', 'CLAUDE_HEADLESS': ''}
REAL_RUN = subprocess.run
SELECTOR = ROOT / 'entry' / 'run.py'
EDIT = {'tool_name': 'write_file', 'args': {'path': 'notes/heron.md', 'content': 'x'}}


def registered():
    hooks = {}

    class Context:
        def register_hook(self, name, callback):
            hooks[name] = callback
    plugin.register(Context())
    return hooks


class HermesCase:
    """Mixin: the registered hooks with the selector children observed."""

    def invoke(self, hook, *, after_classify=None, **kwargs):
        calls = []

        def observed(argv, *args, **kw):
            answer = REAL_RUN(argv, *args, **kw)
            if len(argv) > 1 and str(argv[1]) == str(SELECTOR):
                calls.append({'argv': list(argv[2:4]), 'stdout': answer.stdout.strip(),
                              'returncode': answer.returncode})
                if after_classify is not None:
                    after_classify()
            return answer
        with mock.patch.dict(os.environ, QUIET), \
                mock.patch.object(bridge.subprocess, 'run', side_effect=observed):
            answer = registered()[hook](**kwargs)
        self.assertTrue(calls, 'the registered hook runs the real selector child')
        self.assertTrue(all(call['returncode'] == 0 for call in calls), calls)
        return answer, calls

    def recall(self, **kwargs):
        return self.invoke('pre_llm_call', user_message=PROMPT,
                           working_directory=str(self.project), **kwargs)


class SelectedRoute(HermesCase, HostInstallCase):
    def setUp(self):
        super().setUp()
        self.wakes = Wakes().install(self)

    def test_registered_recall_injects_the_selected_card(self):
        answer, calls = self.recall()
        self.assertEqual(calls[0]['argv'], ['hook-classify', 'recall'])
        self.assertEqual(calls[0]['stdout'], 'candidate')
        self.assertIn('zqheronx', answer['context'])
        self.assertNotIn('request refused', answer['context'])

    def test_session_start_queues_sync_without_a_context_channel(self):
        answer, calls = self.invoke('on_session_start', working_directory=str(self.project))
        self.assertIsNone(answer)
        self.assertEqual((calls[0]['argv'], calls[0]['stdout']), (['classify', 'update'], 'candidate'))
        self.assertIn('laya-sync', self.wakes.kinds())

    def test_edit_gate_continues_and_post_edit_wakes_sync(self):
        answer, _calls = self.invoke('pre_tool_call', working_directory=str(self.project), **EDIT)
        self.assertIsNone(answer)
        self.assertEqual(self.wakes.calls, [])
        self.invoke('post_tool_call', working_directory=str(self.project), result='{}', status='ok',
                    **EDIT)
        self.assertIn('laya-sync', self.wakes.kinds())

    def test_a_route_that_changes_after_classification_is_refused(self):
        route = self.project / '.memvane' / 'route.json'
        answer, calls = self.recall(after_classify=lambda: route.write_bytes(b'{}'))
        self.assertEqual(calls[0]['stdout'], 'candidate')
        self.assertEqual(answer, {'context': '[memvane] selected route changed; request refused'})

    def test_a_damaged_route_refuses_recall_blocks_edits_and_only_warns_otherwise(self):
        (self.project / '.memvane' / 'route.json').write_bytes(b'{}')
        answer, calls = self.recall()
        self.assertEqual(calls[0]['stdout'], 'blocked')
        self.assertEqual(answer, {'context': '[memvane] selected route unavailable; request refused'})
        gate, _calls = self.invoke('pre_tool_call', working_directory=str(self.project), **EDIT)
        self.assertEqual(gate, {'action': 'block', 'message': 'memvane-db route unavailable; edit blocked'})
        with self.assertLogs(bridge.logger, logging.WARNING) as logs:
            self.assertIsNone(self.invoke('on_session_start', working_directory=str(self.project))[0])
            self.assertIsNone(self.invoke('post_tool_call', working_directory=str(self.project),
                                          result='{}', status='ok', **EDIT)[0])
        self.assertEqual([record.getMessage() for record in logs.records],
                         ['[memvane] selected route unavailable; request refused'] * 2)
        self.assertEqual(self.wakes.calls, [])

    def test_a_marker_without_its_route_recalls_nothing(self):
        (self.project / '.memvane' / 'route.json').unlink()
        answer, calls = self.recall()
        self.assertEqual(calls[0]['stdout'], 'legacy')
        self.assertIsNone(answer)


class PreviewRoute(HermesCase, FixtureCase):
    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home)
        note = self.fixture.write_note('notes/heron.md', NOTE)
        self.fixture.write_card('heron.md', title='Emerald heron', body=NOTE, sources=[note])
        self.fixture.commit('fixture')
        self.project = self.fixture.root
        build_install(self, self.fixture, upto='prepared')

    def test_a_preview_recalls_no_context_until_the_build_is_selected(self):
        with mock.patch('memvane.host.api.serve_preview', wraps=host_api.serve_preview) as preview:
            answer, calls = self.recall()
        self.assertEqual(calls[0]['stdout'], 'preview')
        self.assertEqual(answer, {'context': ''})
        self.assertEqual([call.args[:2] for call in preview.call_args_list], [('recall', 'hermes')])

    def test_a_failed_preview_proof_is_quiet(self):
        self.fixture.write_settings(name='renamed')
        answer, calls = self.recall()
        self.assertEqual(calls[0]['stdout'], 'legacy')
        self.assertIsNone(answer)


if __name__ == '__main__':
    unittest.main()
