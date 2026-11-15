"""Recall test seeds: a selected 1.0 install with verified cards, served through the host api.

Every install goes through ``tests.support.install_builder.build_install``.
``recall`` runs the in-process selected recall and returns the context, the
captured stderr and the one ``recall`` event line of that call. Durable wakes
are recorded by a ``runtime.api.request`` stand-in (the fixture switch makes
the real request a no-op).
"""
from __future__ import annotations

from contextlib import redirect_stderr
import io
from unittest import mock

from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

NOTE = 'Field notes: the emerald heron nests beside the reed pond.\n'
TOPICS = ('heron', 'glacier', 'mosaic', 'lantern', 'harbor', 'orchid', 'falcon', 'quarry', 'meadow',
          'beacon', 'canyon', 'dune', 'ember', 'fjord', 'grove', 'island', 'jungle', 'kelp',
          'lagoon', 'marsh')
SHARED = 'zqsharedx'


class RecallInstallCase(FixtureCase):
    """``CARDS`` verified cards (``concepts/<topic>.md``), each naming its own and the shared marker."""

    CARDS = 1

    def setUp(self):
        super().setUp()
        from memvane.kernel import hook_budget
        # In process nothing arms the hook budget (as for Hermes).
        budget = mock.patch.object(hook_budget, '_deadline', None)
        budget.start()
        self.addCleanup(budget.stop)
        self.fixture = make_project(self.home)
        self.note = self.fixture.write_note('notes.md', NOTE)
        for topic in TOPICS[:self.CARDS]:
            self.write_card(topic)
        self.fixture.commit('fixture')
        self.project = self.fixture.root
        self.install = build_install(self, self.fixture)
        self.install.settle()
        self.wakes = []
        patcher = mock.patch('memvane.runtime.api.request', side_effect=self._request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _request(self, kind, target='', **kwargs):
        self.wakes.append((kind, target, kwargs))
        return {'status': 'queued'}

    def write_card(self, topic):
        return self.fixture.write_card(
            f'concepts/{topic}.md', title=topic.title() + ' protocol', sources=[self.note],
            body=f'The {topic} protocol keeps marker token zq{topic}x and {SHARED} for recall checks.\n')

    def recall(self, prompt, host='claude', session='recall-fixture', cwd=None):
        """``(context, stderr, event)`` of one in-process selected recall."""
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        cwd = str(cwd or self.project)
        decision = select_route(cwd, action='recall')
        self.assertIn(decision.get('state'), ('candidate', 'worktree'), decision)
        before = len(self.install.events())
        err = io.StringIO()
        with redirect_stderr(err):
            answer = serve_selected('recall', host, decision, {
                'session_id': session, 'cwd': cwd, 'hook_event_name': 'UserPromptSubmit',
                'prompt': prompt})
        if host == 'hermes':
            text = (answer or {}).get('context', '')
        else:
            text = ((answer or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
        lines = [line for line in self.install.events()[before:] if line.get('event') == 'recall']
        self.assertEqual(len(lines), 1, lines)
        return text, err.getvalue(), lines[0]
