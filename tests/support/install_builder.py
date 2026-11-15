"""S7: a project onboarded through the 1.0 install use cases only.

``build_install(case, project, upto=...)`` runs ``memvane.install.api`` plan,
prepare, initial build passes (``sync.api.run``), finalize and activate, in
that order, up to ``upto`` (``'planned'|'prepared'|'ready'|'finalized'|'selected'``),
asserting each step's status. The provider is a ``FixtureProvider`` (installed
at the test runtime root once per test case). ``Install`` also exposes every
step on its own so lifecycle tests can stop one at a durable point and rerun it.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
STEPS = ('planned', 'prepared', 'ready', 'finalized', 'selected')
BUILD_PASSES = 8
SETTLE_PASSES = 6
HOOK_TIMEOUT = 60


def fixture_provider(case, dimension=2):
    """The case's fixture provider port, installed at its test runtime root on first use."""
    provider = getattr(case, 'fixture_provider', None)
    if provider is None:
        from tests.support.fake_provider import FixtureProvider
        provider = case.fixture_provider = FixtureProvider(dimension).install(case)
    return provider


class Install:
    def __init__(self, case, project, install_id, provider):
        from memvane.kernel.memvane_paths import install_root
        self.case = case
        self.project = project
        self.install_id = install_id
        self.provider = provider
        self.state = install_root(install_id)
        self.journal = self.state / 'journal.sqlite'
        self.store_path = self.state / 'store.sqlite'
        self.decision = None
        self.plan_result = None

    # -- steps (raw results; the caller asserts) ---------------------------------
    def plan(self):
        from memvane.install import api as install
        self.plan_result = install.plan_fresh_wiki_origin(self.project.root)
        return self.plan_result

    def prepare(self):
        from memvane.install import api as install
        from tests.support.fake_provider import REVIEW_SHA
        plan = self.plan()
        return install.prepare_fresh_wiki_origin(
            project_root=str(self.project.root), plugin_root=str(ROOT), state_root=str(self.state),
            expected_proposal_sha256=plan['proposalSha256'], install_id=self.install_id,
            accepted_provider_sha256=REVIEW_SHA, dimension=self.provider.dimension,
            provider=self.provider)

    def build(self, limit=BUILD_PASSES):
        """Initial build passes until the build is ready; returns every pass result."""
        from tests.support.sync_harness import passes_until
        return passes_until(self, lambda result: result.get('buildPhase') == 'ready', limit)

    def finalize(self, resolve_sync_target=None):
        from memvane.install import api as install
        from memvane.journal import api as journal
        if resolve_sync_target is None:
            def resolve_sync_target(project):
                return journal.dispatch_target(Path(project), build=True)
        return install.finalize_initial_build(project_root=str(self.project.root),
                                              state_root=str(self.state),
                                              resolve_sync_target=resolve_sync_target)

    def activate(self):
        from memvane.install import api as install
        route = (self.state / 'route.json').read_bytes()
        with install.install_lock(self.state):
            result = install.activate_origin_selection(self.project.root, self.journal, route)
        if result.get('status') == 'selected':
            self.select()
        return result

    def select(self):
        """The recall route decision of the selected install (``journalPath``, ``storePath``)."""
        from memvane.journal.api import select_route
        self.decision = select_route(str(self.project.root), action='recall')
        if self.decision.get('state') == 'candidate':
            self.journal = Path(self.decision['journalPath'])
            self.store_path = Path(self.decision['storePath'])
        return self.decision

    def restore(self):
        from memvane.install import api as install
        return install.restore_origin_selection(project_root=str(self.project.root),
                                                state_root=str(self.state))

    # -- passes --------------------------------------------------------------------
    def run_sync(self):
        from tests.support.sync_harness import run_pass
        return run_pass(self)

    def epoch(self):
        from memvane.journal.api import inspect_journal
        return inspect_journal(self.journal)['epoch']

    def settle(self, limit=SETTLE_PASSES):
        """Run passes until one changes nothing: no journal fence, unchanged view."""
        results = []
        for _ in range(limit):
            before = self.epoch()
            results.append(self.run_sync())
            if results[-1].get('state') == 'unchanged' and self.epoch() == before:
                return results[-1]
        raise AssertionError('sync passes did not settle: %r' % (results,))

    # -- observation -----------------------------------------------------------------
    def db(self):
        """A read-only connection to the install's store (closed by ``with``)."""
        from memvane.kernel.errors import _query_only_uri
        connection = sqlite3.connect(_query_only_uri(self.store_path), uri=True)
        connection.execute('PRAGMA query_only=ON')
        return closing(connection)

    def events(self):
        path = self.state / 'events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]

    def recall(self, prompt, host='claude', session='fixture-session'):
        """In-process selected recall: the injected context ('' when none)."""
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        decision = select_route(str(self.project.root), action='recall')
        if decision.get('state') != 'candidate':
            raise AssertionError('recall route not selected: %r' % (decision,))
        served = serve_selected('recall', host, decision, {
            'session_id': session, 'cwd': str(self.project.root),
            'hook_event_name': 'UserPromptSubmit', 'prompt': prompt})
        return ((served or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')

    def hook(self, action, payload, host='claude'):
        """``entry/run.py hook-serve`` in a child process: ``(exit code, stdout, stderr)``."""
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('QMD_SANDBOX', None)
        result = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'entry' / 'run.py'), 'hook-serve', action, host,
             str(self.project.root)],
            input=json.dumps(payload).encode('utf-8'), capture_output=True, timeout=HOOK_TIMEOUT,
            env=env, cwd=str(self.project.root))
        return result.returncode, result.stdout.decode('utf-8'), result.stderr.decode('utf-8')


def build_install(case, project, *, upto='selected', install_id='4' * 32, provider=None):
    if upto not in STEPS:
        raise ValueError('upto must be one of %r' % (STEPS,))
    install = Install(case, project, install_id, provider or fixture_provider(case))
    last = STEPS.index(upto)
    plan = install.plan()
    case.assertEqual(plan.get('status'), 'review_required', plan)
    if last >= 1:
        prepared = install.prepare()
        case.assertEqual(prepared.get('status'), 'initial_build_registered', prepared)
    if last >= 2:
        install.build()
    if last >= 3:
        finalized = install.finalize()
        case.assertEqual(finalized.get('status'), 'initial_build_finalized', finalized)
    if last >= 4:
        selected = install.activate()
        case.assertEqual(selected.get('status'), 'selected', selected)
        case.assertEqual(install.decision.get('state'), 'candidate', install.decision)
    return install


def build_out_of_project(case, n, *, name='fixture'):
    """``n`` projects under the case HOME, each onboarded and selected with its own install id."""
    from tests.support.project import make_project
    if not 1 <= n <= 12:
        raise ValueError('1 <= n <= 12')
    installs = []
    for index in range(n):
        project = make_project(case.home, name=f'{name}{index}', dirname=f'project{index}')
        project.write_note('notes.md', f'Field notes of project {index}.\n')
        project.commit()
        installs.append(build_install(case, project, install_id='%x' % (index + 4) * 32))
    return installs
