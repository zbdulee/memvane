#!/usr/bin/env python3
"""Selected-route wiki lifecycle triggers, from the hook to the compile pass.

A selected SessionStart, an edit inside the project and a successful agent
Bash record a lifecycle request and offer the project's `laya-compile` wake.
Draining a real wake queue runs that pass: the lifecycle retires a card whose
source was deleted, backfills a legacy card and enqueues a changed card, and
the compile worker of the same pass consumes the regeneration row. A rebase
in progress, a linked worktree and an unselected project change nothing.

Every project, HOME, state dir and wake queue is a temp directory. Only the
installer proof (`verify_selected_route`, `wiki_selected_laya.selected`), the
pinned search adapter and sealed-bundle hop are stood in for; child entry points run in-process so
those stand-ins reach them.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CORE = REPO / 'core'
sys.path.insert(0, str(CORE))
import laya_host_response as response  # noqa: E402
import laya_install_journal  # noqa: E402
import runtime_work  # noqa: E402
import wiki_selected_laya  # noqa: E402
import wiki_compile_retrieval  # noqa: E402
import yaml_scalars  # noqa: E402

GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'fixture',
           'GIT_AUTHOR_EMAIL': 'fixture@example.invalid', 'GIT_COMMITTER_NAME': 'fixture',
           'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
REAL_RUN = subprocess.run
FAKE_CLI = '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_CLI_SEEN'], 'a', encoding='utf-8') as handle:
    handle.write(sys.argv[-1] + '\\n---\\n')
print(json.dumps({'candidates': [{
    'title': 'Changed Design', 'summary': 'Regenerated from the edited design note.',
    'suggestedType': 'concept', 'confidence': 'high',
    'targetPath': '.auto-context/wiki/concepts/changed-card.md'}]}))
'''


def git(cwd, *args, ok=(0,)):
    result = REAL_RUN(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                       '-c', 'init.defaultBranch=main', *args],
                      cwd=str(cwd), env={**os.environ, **GIT_ENV},
                      capture_output=True, text=True, check=False)
    if result.returncode not in ok:
        raise AssertionError(f'git {args} failed: {result.stderr}')
    return result


class SelectedLifecycle(unittest.TestCase):
    """Minimal-settings selected project (temp HOME) that is a real git repository."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lifecycle-trigger-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.root = self.home / 'project'
        self.wiki = self.root / '.auto-context/wiki'
        (self.wiki / 'concepts').mkdir(parents=True)
        (self.root / 'docs').mkdir()
        self.state = self.base / 'state'
        self.log = self.base / 'lifecycle.log'
        self.seen = self.base / 'extractor-seen.txt'
        fake = self.base / 'fake-claude'
        fake.write_text(FAKE_CLI)
        fake.chmod(0o755)
        env = mock.patch.dict(os.environ, {
            **GIT_ENV, 'HOME': str(self.home), 'QMD_SYNC_STATE_DIR': str(self.state),
            'QMD_WIKI_LIFECYCLE_LOG': str(self.log), 'QMD_SOURCE_SCAN_MAX': '',
            'QMD_ENGINE': 'claude', 'QMD_EXTRACTOR_CLAUDE_BIN': str(fake),
            'FAKE_CLI_SEEN': str(self.seen), 'QMD_RECALL_LOG': ''})
        env.start()
        self.addCleanup(env.stop)
        (self.root / '.auto-context/settings.json').write_text(
            json.dumps({'name': 'project', 'skipPaths': []}) + '\n')
        self.collections = [
            {'name': 'project', 'role': 'raw', 'sourceRoot': str(self.root)},
            {'name': 'project-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}]
        self.decision = {'state': 'candidate', 'projectRoot': str(self.root),
                         'pluginRoot': str(REPO), 'collections': self.collections}
        self.bound = ({**self.decision, 'installId': 'i' * 32, 'generation': 'g1',
                       'scopeId': 's' * 32, 'dbIncarnation': 'd' * 32,
                       'recallSettings': {'skipPaths': []},
                       'collectionRoots': {'project': str(self.root),
                                           'project-wiki': str(self.wiki)}}, {})
        self.proof = {'productRouteAllowed': True, 'selectionEpoch': 1}
        queue_root = self.base / 'wakes'
        queue_root.mkdir(mode=0o700)
        self.queue = runtime_work.WakeQueue(queue_root)
        self.requests = []
        self.passes = []

    # --- project content -------------------------------------------------
    def source(self, name, body):
        path = self.root / 'docs' / name
        path.write_bytes(body)
        return path

    def card(self, name, *, source=None, body=None, legacy_source=None):
        lines = ['---', 'title: Card ' + name, 'status: generated',
                 'createdBy: qmd-auto-context']
        if legacy_source is not None:
            lines += ['sources:',
                      '  - {kind: "file", path: "docs/%s", collection: "project"}' % legacy_source,
                      'triggers:', '  - post_tool_source']
        else:
            revision = {'kind': 'file', 'path': 'docs/' + source, 'collection': 'project',
                        'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body),
                        'mtimeNs': 1}
            lines.append('sourceRevisions:')
            lines += ['  - ' + item for item in yaml_scalars.dump_source_revisions([revision])]
        lines += ['---', '', '<!-- qmd:auto:start id="main" sourceHash="a" -->', '## Summary',
                  'fact from ' + name, '<!-- qmd:auto:end -->', '']
        path = self.wiki / 'concepts' / name
        path.write_text('\n'.join(lines), encoding='utf-8')
        return path

    def commit(self, message, *paths):
        git(self.root, 'add', '--all', '--', *paths)
        git(self.root, 'commit', '-q', '-m', message)

    def three_cards(self):
        """A deleted source, a legacy card and a changed source, all committed."""
        git(self.root, 'init', '-q')
        gone_body, changed_body = b'retired design note\n', b'design note v1\n'
        self.source('gone.md', gone_body)
        self.source('legacy.md', b'legacy design note\n')
        self.source('changed.md', changed_body)
        cards = {'gone': self.card('gone-card.md', source='gone.md', body=gone_body),
                 'legacy': self.card('legacy-card.md', legacy_source='legacy.md'),
                 'changed': self.card('changed-card.md', source='changed.md',
                                      body=changed_body)}
        self.commit('sources and cards', '.')
        git(self.root, 'rm', '-q', 'docs/gone.md')
        self.source('changed.md', b'design note v2: the edited decision\n')
        self.commit('delete one source, edit another', '.')
        return cards

    # --- runtime stand-ins -------------------------------------------------
    def fake_request(self, kind, target='', *, start=True, pull_due=False):
        self.requests.append((kind, target))
        offered = self.queue.offer(kind, target)
        return {'status': {'queued': 'queued', 'retained': 'deferred_queue_capacity_retained',
                           'dropped': 'deferred_queue_capacity'}[offered]}

    def child(self, argv, *args, **kwargs):
        """In-process lifecycle / compile worker children; everything else is real."""
        script = Path(argv[2]).name if len(argv) > 2 else ''
        if script not in ('wiki_lifecycle.py', 'wiki_compile_worker.py'):
            return REAL_RUN(argv, *args, **kwargs)
        module = __import__(script[:-3])
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', [script, *argv[3:]]), \
                mock.patch.dict(os.environ, {'QMD_RUNTIME_DISPATCH': '1'}), \
                contextlib.redirect_stdout(out):
            code = module.main()
        self.passes.append(script)
        return subprocess.CompletedProcess(argv, code or 0, out.getvalue(), '')

    def dispatch(self, row):
        if row['kind'] == 'laya-sync':
            return True
        if row['kind'] == 'laya-compile':
            return wiki_selected_laya.run(Path(row['target']))['state'] == 'drained'
        raise AssertionError('unexpected wake ' + row['kind'])

    @contextlib.contextmanager
    def selected_runtime(self):
        @contextlib.contextmanager
        def no_published_index(*_args):
            # This lifecycle fixture has no installed DB. The real live-card
            # overlay still runs; pinned search is covered by its own fixture.
            yield [], (frozenset(), frozenset()), 'project-wiki', 'bound'

        with mock.patch.object(laya_install_journal, 'verify_selected_route',
                               return_value=self.proof), \
                mock.patch.object(wiki_selected_laya, 'selected', return_value=self.bound), \
                mock.patch.object(wiki_selected_laya, 'recheck'), \
                mock.patch.object(wiki_compile_retrieval, 'selected_search',
                                  no_published_index), \
                mock.patch.object(runtime_work, 'request', side_effect=self.fake_request), \
                mock.patch.object(runtime_work, 'hook_wake'), \
                mock.patch.object(subprocess, 'run', side_effect=self.child):
            yield

    def hook(self, action, payload):
        with self.selected_runtime():
            return response.serve_selected(action, 'claude', self.decision, payload)

    def drain(self):
        with self.selected_runtime():
            return runtime_work.drain(self.queue, execute=self.dispatch, ready=lambda: True)

    def session_start(self):
        return self.hook('update-queued', {'hook_event_name': 'SessionStart',
                                           'cwd': str(self.root)})

    def kinds(self):
        return [kind for kind, _ in self.requests]

    def queue_rows(self):
        path = self.root / '.auto-context/compile/source-queue.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ''


class SessionStartRunsLifecycle(SelectedLifecycle):
    def test_session_start_retires_backfills_and_regenerates_in_one_compile_pass(self):
        cards = self.three_cards()
        out = self.session_start()
        self.assertIn('update queued', out['hookSpecificOutput']['additionalContext'])
        self.assertIn(('laya-compile', str(self.root)), self.requests)
        self.assertIn('laya-compile', [row['kind'] for row in self.queue.pending()])
        drained = self.drain()
        self.assertEqual(drained['status'], 'drained', drained)
        self.assertEqual(self.queue.pending(), [])
        log = self.log_text()
        # Deleted source in HEAD's lineage: archived and retired, sync woken.
        self.assertFalse(cards['gone'].exists(), log)
        self.assertIn('RETIRE source_missing', log)
        self.assertEqual(len(list(self.root.glob('.auto-context/compile/retired-wiki/*.md'))), 1)
        # Legacy `sources:` card gains compiler-owned provenance.
        self.assertIn('BACKFILL source_revisions', log)
        self.assertIn('sourceRevisions:', cards['legacy'].read_text())
        # Changed source: minimal settings enqueue without a trigger grant ...
        self.assertRegex(log, r'ENQUEUE (queued|capped): queued=1')
        # ... and the same pass's compile worker consumes that row.
        self.assertEqual(self.passes[:2], ['wiki_lifecycle.py', 'wiki_compile_worker.py'],
                         self.passes)
        self.assertEqual(self.queue_rows(), [])
        self.assertIn('design note v2: the edited decision', self.seen.read_text())
        # The retirement's own laya-sync wake went into the same queue and was served.
        self.assertGreaterEqual(self.kinds().count('laya-sync'), 2, self.requests)

    def test_repeated_session_start_is_served_by_one_run(self):
        self.three_cards()
        self.session_start()
        self.session_start()
        self.assertEqual(self.kinds().count('laya-compile'), 1, self.requests)
        self.drain()
        self.assertEqual(self.passes.count('wiki_lifecycle.py'), 1, self.passes)
        # Served: the next SessionStart records a new request and wake.
        before = len(self.requests)
        self.session_start()
        self.assertEqual(self.kinds()[before:].count('laya-compile'), 1, self.requests)

    def test_failed_lifecycle_child_still_drains_the_compile_pass(self):
        cards = self.three_cards()
        self.session_start()
        real_child = self.child

        def wedged(argv, *args, **kwargs):
            if len(argv) > 2 and Path(argv[2]).name == 'wiki_lifecycle.py':
                raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
            return real_child(argv, *args, **kwargs)
        self.child = wedged
        drained = self.drain()
        self.assertEqual(drained['status'], 'drained', drained)
        self.assertEqual(self.queue.pending(), [])
        self.assertTrue(cards['gone'].exists())
        self.assertIn('EXCEPTION: lifecycle child TimeoutExpired', self.log_text())
        # The failed request is not retried; the next trigger records a new one.
        before = len(self.requests)
        self.session_start()
        self.assertIn('laya-compile', self.kinds()[before:])

    def test_long_pending_request_offers_its_wake_again(self):
        self.three_cards()
        self.session_start()
        import wiki_lifecycle_request
        with mock.patch.object(wiki_lifecycle_request.time, 'time_ns',
                               return_value=wiki_lifecycle_request.time.time_ns()
                               + (wiki_lifecycle_request.STALE_SECONDS + 1) * 10**9):
            self.session_start()
        self.assertEqual(self.kinds().count('laya-compile'), 2, self.requests)

    def test_lifecycle_request_failure_never_changes_the_hook_answer(self):
        self.three_cards()
        baseline = self.session_start()
        import wiki_lifecycle_request
        with mock.patch.object(wiki_lifecycle_request, 'record',
                               side_effect=OSError('state dir unwritable')):
            self.assertEqual(self.session_start(), baseline)


class SourceEditRunsLifecycle(SelectedLifecycle):
    def edit(self, rel):
        return {'hook_event_name': 'PostToolUse', 'tool_name': 'Write',
                'tool_input': {'file_path': rel}, 'cwd': str(self.root)}

    def test_edit_inside_project_triggers_lifecycle(self):
        cards = self.three_cards()
        self.assertEqual(self.hook('index', self.edit('docs/changed.md')), {})
        self.assertEqual(self.kinds()[:2], ['laya-compile', 'laya-sync'], self.requests)
        self.drain()
        self.assertFalse(cards['gone'].exists(), self.log_text())
        self.assertIn('wiki_lifecycle.py', self.passes)

    def test_successful_bash_triggers_lifecycle_and_a_burst_coalesces(self):
        cards = self.three_cards()
        bash = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'cwd': str(self.root),
                'tool_input': {'command': 'git rm docs/gone.md'},
                'tool_response': {'exit_code': 0}}
        for _ in range(3):
            self.assertEqual(self.hook('file-change-event', bash), {})
        self.assertEqual(self.kinds().count('laya-compile'), 1, self.requests)
        self.assertEqual(self.kinds().count('laya-sync'), 3, self.requests)
        self.drain()
        self.assertFalse(cards['gone'].exists(), self.log_text())
        self.assertEqual(self.passes.count('wiki_lifecycle.py'), 1, self.passes)


class GitSafety(SelectedLifecycle):
    def test_rebase_in_progress_retires_nothing_and_acks_the_pass(self):
        git(self.root, 'init', '-q')
        (self.root / 'conflict.txt').write_text('base\n')
        self.commit('conflict base', 'conflict.txt')
        git(self.root, 'checkout', '-q', '-b', 'topic')
        (self.root / 'conflict.txt').write_text('topic\n')
        self.commit('topic edit', 'conflict.txt')
        git(self.root, 'checkout', '-q', 'main')
        (self.root / 'conflict.txt').write_text('main\n')
        self.commit('main edit', 'conflict.txt')
        body = b'deleted for real\n'
        self.source('gone.md', body)
        card = self.card('gone-card.md', source='gone.md', body=body)
        self.commit('add source and card', '.')
        git(self.root, 'rm', '-q', 'docs/gone.md')
        git(self.root, 'commit', '-q', '-m', 'delete source')
        git(self.root, 'checkout', '-q', 'topic')
        git(self.root, 'rebase', 'main', ok=(1,))
        try:
            self.session_start()
            self.assertIn(('laya-compile', str(self.root)), self.requests)
            drained = self.drain()
            self.assertEqual(drained['status'], 'drained', drained)
            self.assertEqual(self.queue.pending(), [])
            self.assertIn('wiki_lifecycle.py', self.passes)
            self.assertTrue(card.exists(), self.log_text())
            self.assertIn('in progress', self.log_text())
            self.assertEqual(list(self.root.glob('.auto-context/compile/retired-wiki/*')), [])
        finally:
            git(self.root, 'rebase', '--abort')

    def test_linked_worktree_route_records_no_lifecycle_request(self):
        git(self.root, 'init', '-q')
        self.commit('settings', '.auto-context/settings.json')
        worktree = self.base / 'linked'
        git(self.root, 'worktree', 'add', '-q', '-b', 'side', str(worktree))
        decision = {**self.decision, 'state': 'worktree', 'worktreeRoot': str(worktree),
                    'mainRoute': self.decision}
        import wiki_lifecycle_request
        edit = {'hook_event_name': 'PostToolUse', 'tool_name': 'Write',
                'tool_input': {'file_path': str(worktree / 'a.md')}, 'cwd': str(worktree)}
        with self.selected_runtime(), \
                mock.patch.object(laya_install_journal, 'verify_selected_route_read_only',
                                  return_value={'readOnlyAllowed': True}), \
                mock.patch.object(wiki_lifecycle_request, 'record',
                                  side_effect=AssertionError('worktree recorded a request')):
            out = response.serve_selected('update-queued', 'claude', decision,
                                          {'hook_event_name': 'SessionStart',
                                           'cwd': str(worktree)})
            self.assertIn('linked git worktree', out['hookSpecificOutput']['additionalContext'])
            for action in ('index', 'file-change-event'):
                self.assertEqual(response.serve_selected(action, 'claude', decision, edit), {})
        self.assertEqual(self.requests, [])


class UnselectedProject(unittest.TestCase):
    def test_unselected_session_start_records_nothing(self):
        with tempfile.TemporaryDirectory(prefix='lifecycle-unselected-') as temp:
            home = Path(temp).resolve()
            project = home / 'project'
            (project / '.auto-context/wiki').mkdir(parents=True)
            (project / '.auto-context/settings.json').write_text(
                json.dumps({'name': 'project', 'skipPaths': []}))
            state = home / 'state'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('CLAUDE', 'CODEX', 'QMD_'))}
            env.update(HOME=str(home), QMD_SYNC_STATE_DIR=str(state), QMD_RECALL_LOG='',
                       QMD_SETUP_GUARD_FIXTURE='1', PYTHONDONTWRITEBYTECODE='1')
            out = REAL_RUN([str(REPO / 'hooks/run-hook'), 'update-queued', 'claude'],
                           input=json.dumps({'hook_event_name': 'SessionStart',
                                             'cwd': str(project)}),
                           cwd=str(project), env=env, capture_output=True, text=True,
                           timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            written = sorted(str(path.relative_to(home)) for path in home.rglob('*')
                             if 'lifecycle' in path.name or path.name.startswith('runtime-wake'))
            self.assertEqual(written, [])


if __name__ == '__main__':
    unittest.main()
