"""Host test seeds: a selected 1.0 install with one verified card, and wake recording.

Every install goes through ``tests.support.install_builder.build_install``.
Under ``QMD_SETUP_GUARD_FIXTURE=1`` ``runtime.api.request`` records nothing and
returns None, so a test that needs a recorded durable wake installs ``Wakes``
(a ``runtime.api.request`` stand-in that answers ``queued``).
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from unittest import mock

from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

NOTE = 'The emerald heron nests beside the north pond.\n'
CARD_BODY = 'The emerald heron keeps marker token zqheronx beside the north pond.\n'
PROMPT = 'what marker does the emerald heron keep?'


class Wakes:
    """Records ``runtime.api.request`` calls; each answers ``{'status': status}``."""

    def __init__(self, status='queued'):
        self.status = status
        self.calls = []

    def __call__(self, kind, target='', **kwargs):
        self.calls.append((kind, target, kwargs))
        return {'status': self.status} if self.status is not None else None

    def install(self, case):
        patcher = mock.patch('memvane.runtime.api.request', side_effect=self)
        patcher.start()
        case.addCleanup(patcher.stop)
        return self

    def kinds(self):
        return [kind for kind, _target, _kwargs in self.calls]


class HostInstallCase(FixtureCase):
    """A project with a note and one verified card, onboarded, selected and settled."""

    def setUp(self):
        super().setUp()
        from memvane.kernel import hook_budget
        # In process nothing arms the hook budget (as for Hermes); a ``main``
        # call of another test must not leave its module deadline behind.
        budget = mock.patch.object(hook_budget, '_deadline', None)
        budget.start()
        self.addCleanup(budget.stop)
        self.fixture = make_project(self.home)
        note = self.fixture.write_note('notes/heron.md', NOTE)
        self.fixture.write_card('heron.md', title='Emerald heron', body=CARD_BODY, sources=[note])
        self.fixture.commit('fixture')
        self.project = self.fixture.root
        self.install = build_install(self, self.fixture)
        self.install.settle()
        self.decision = self.install.select()

    def select(self, action='recall', cwd=None):
        from memvane.journal.api import select_route
        return select_route(str(cwd or self.project), action=action)

    def serve(self, action, payload, host='claude', cwd=None):
        """In-process ``serve_selected`` for a freshly selected decision of ``action``."""
        from memvane.host.api import serve_selected
        cwd = str(cwd or self.project)
        return serve_selected(action, host, self.select(action, cwd), {'cwd': cwd, **payload})

    def main(self, action, payload, host='claude'):
        """``host_response.main`` in process: ``(exit code, stdout, stderr)``."""
        from memvane.host import host_response
        path = self.tmp / ('payload-%s.json' % action)
        path.write_text(json.dumps({'cwd': str(self.project), **payload}), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_response.main(['serve-hook', action, host, str(path), str(self.project)])
        return code, out.getvalue(), err.getvalue()


def context(answer, host='claude'):
    """The injected recall context of a host answer ('' when none)."""
    if host == 'hermes':
        return (answer or {}).get('context', '')
    return ((answer or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')

