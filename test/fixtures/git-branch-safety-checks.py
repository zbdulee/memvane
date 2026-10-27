#!/usr/bin/env python3
"""Branch and worktree safety against real temporary git repositories.

Lifecycle: a wiki card whose source is only invisible on the checked-out
branch is held; a deletion committed in HEAD's lineage retires; a rebase in
progress or a linked worktree skips the run. Recall: a linked worktree of a
selected main checkout is served from that checkout's published generation
read-only, with no wake, grant, learning capture or setup prompt.

Every repository, HOME and state dir is a temp directory. Git runs with no
global or system config so signing, hooks and default-branch settings of the
host never apply.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
CORE = HERE.parents[1] / 'core'
sys.path.insert(0, str(CORE))
import wiki_lifecycle as lifecycle  # noqa: E402
import wiki_selected_laya  # noqa: E402
import wiki_source_retire  # noqa: E402
import yaml_scalars  # noqa: E402

GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'fixture',
           'GIT_AUTHOR_EMAIL': 'fixture@example.invalid', 'GIT_COMMITTER_NAME': 'fixture',
           'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


def git(cwd, *args, ok=(0,)):
    result = subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                             '-c', 'init.defaultBranch=main', *args],
                            cwd=str(cwd), env={**os.environ, **GIT_ENV},
                            capture_output=True, text=True, check=False)
    if result.returncode not in ok:
        raise AssertionError(f'git {args} failed: {result.stderr}')
    return result


class GitProject(unittest.TestCase):
    """A selected-route-shaped project that is a real git repository."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='git-branch-safety-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'project'
        self.wiki = self.root / '.auto-context/wiki'
        (self.wiki / 'concepts').mkdir(parents=True)
        (self.root / 'docs').mkdir()
        self.log = self.base / 'lifecycle.log'
        env = mock.patch.dict(os.environ, {**GIT_ENV,
                                           'QMD_SYNC_STATE_DIR': str(self.base / 'state'),
                                           'QMD_WIKI_LIFECYCLE_LOG': str(self.log),
                                           'QMD_SOURCE_SCAN_MAX': '', 'QMD_ENGINE': 'claude'})
        env.start()
        self.addCleanup(env.stop)
        # Per-process judge cache; absent before the branch-safety change.
        getattr(wiki_source_retire, '_LINEAGE_CACHE', {}).clear()
        (self.root / '.auto-context/settings.json').write_text(json.dumps({
            'indexing': True,
            'collections': ['proj-docs', 'proj-wiki'],
            'collectionPaths': {'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki'},
            'collectionRoles': {'proj-docs': 'raw', 'proj-wiki': 'wiki'},
            'wikiPath': '.auto-context/wiki',
            'compile': {'mode': 'auto-wiki',
                        'triggers': ['session_start_source', 'post_tool_source']}}))
        self.source('base.md', b'base body\n')
        git(self.root, 'init', '-q')
        self.commit('initial', '.')
        self.selected = ({
            'projectRoot': str(self.root),
            'installId': 'i' * 32, 'generation': 'g1', 'scopeId': 's' * 32,
            'dbIncarnation': 'd' * 32, 'recallSettings': {'skipPaths': []},
            'collectionRoots': {'proj-wiki': str(self.wiki),
                                'proj-docs': str(self.root / 'docs')},
            'collections': [{'name': 'proj-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)},
                            {'name': 'proj-docs', 'role': 'raw',
                             'sourceRoot': str(self.root / 'docs')}]}, {})

    def commit(self, message, *paths):
        git(self.root, 'add', '--all', '--', *paths)
        git(self.root, 'commit', '-q', '-m', message)

    def source(self, name, body):
        path = self.root / 'docs' / name
        path.write_bytes(body)
        return path

    def card(self, name, source_name, body):
        revision = {'kind': 'file', 'path': 'docs/' + source_name, 'collection': 'proj-docs',
                    'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body),
                    'mtimeNs': 1}
        lines = ['---', 'title: Card ' + name, 'status: generated',
                 'createdBy: qmd-auto-context', 'sourceRevisions:']
        lines += ['  - ' + item for item in yaml_scalars.dump_source_revisions([revision])]
        lines += ['---', '', '<!-- qmd:auto:start id="main" sourceHash="a" -->', '## Summary',
                  'fact from ' + name, '<!-- qmd:auto:end -->', '']
        path = self.wiki / 'concepts' / name
        path.write_text('\n'.join(lines), encoding='utf-8')
        return path

    def run_lifecycle(self, cwd=None):
        with mock.patch.object(wiki_selected_laya, 'selected', return_value=self.selected), \
                mock.patch.object(wiki_selected_laya, 'recheck'), \
                mock.patch.object(wiki_selected_laya, 'request_sync', return_value=True), \
                mock.patch.object(wiki_selected_laya, 'request_compile'):
            return lifecycle.run(str(cwd or self.root))

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ''


class LifecycleBranchSafety(GitProject):
    def test_branch_switch_keeps_uncommitted_card_whose_source_is_only_on_main(self):
        git(self.root, 'branch', 'feature')
        body = b'main-only design note\n'
        self.source('only-main.md', body)
        self.commit('add main-only source', 'docs/only-main.md')
        card = self.card('only-main-card.md', 'only-main.md', body)  # generated, not committed
        git(self.root, 'checkout', '-q', 'feature')
        self.assertFalse((self.root / 'docs/only-main.md').exists())
        self.assertTrue(card.exists(), 'an untracked card survives the checkout')
        result = self.run_lifecycle()
        self.assertTrue(card.exists(), self.log_text())
        self.assertEqual(result['retired'], 0)
        self.assertEqual(result['held'], 1)
        self.assertIn('HOLD source_not_deleted_in_lineage', self.log_text())
        self.assertIn('source_on_other_branch', self.log_text())
        self.assertEqual(list(self.root.glob('.auto-context/compile/retired-wiki/*.md')), [])

    def test_source_delete_job_on_other_branch_is_held_not_retired(self):
        git(self.root, 'branch', 'feature')
        body = b'main-only design note\n'
        self.source('only-main.md', body)
        self.commit('add main-only source', 'docs/only-main.md')
        card = self.card('only-main-card.md', 'only-main.md', body)
        git(self.root, 'checkout', '-q', 'feature')
        with mock.patch.object(wiki_selected_laya, 'recheck'), \
                mock.patch.object(wiki_selected_laya, 'request_sync', return_value=True):
            outcome = wiki_source_retire.retire_deleted_source(
                self.root, json.loads((self.root / '.auto-context/settings.json').read_text()),
                self.selected, 'proj-docs', 'docs/only-main.md',
                hashlib.sha256(body).hexdigest())
        self.assertEqual(outcome['state'], 'held_branch', outcome)
        self.assertEqual(outcome['reason'], 'source_on_other_branch')
        self.assertTrue(card.exists())

    def test_deletion_committed_on_current_branch_retires_committed_card(self):
        body = b'retired design note\n'
        self.source('gone.md', body)
        card = self.card('gone-card.md', 'gone.md', body)
        self.commit('add source and card', '.')
        git(self.root, 'rm', '-q', 'docs/gone.md')
        git(self.root, 'commit', '-q', '-m', 'delete source')
        result = self.run_lifecycle()
        self.assertFalse(card.exists(), self.log_text())
        self.assertEqual(result['retired'], 1)
        self.assertEqual(result.get('held', 0), 0)

    def test_deletion_committed_on_current_branch_retires_uncommitted_card(self):
        body = b'retired design note\n'
        self.source('gone.md', body)
        self.commit('add source', 'docs/gone.md')
        card = self.card('gone-card.md', 'gone.md', body)
        git(self.root, 'rm', '-q', 'docs/gone.md')
        git(self.root, 'commit', '-q', '-m', 'delete source')
        result = self.run_lifecycle()
        self.assertFalse(card.exists(), self.log_text())
        self.assertEqual(result['retired'], 1)

    def test_uncommitted_working_tree_deletion_is_held(self):
        body = b'still tracked\n'
        self.source('tracked.md', body)
        card = self.card('tracked-card.md', 'tracked.md', body)
        self.commit('add source and card', '.')
        (self.root / 'docs/tracked.md').unlink()
        result = self.run_lifecycle()
        self.assertTrue(card.exists())
        self.assertEqual(result['held'], 1)
        self.assertIn('source_tracked_in_head', self.log_text())

    def test_never_tracked_source_retires_only_a_committed_card(self):
        body = b'local scratch\n'
        committed = self.card('committed-card.md', 'scratch.md', body)
        self.commit('card only', '.auto-context/wiki')
        loose = self.card('loose-card.md', 'scratch2.md', body)
        result = self.run_lifecycle()
        self.assertFalse(committed.exists(), self.log_text())
        self.assertTrue(loose.exists())
        self.assertEqual(result['retired'], 1)
        self.assertEqual(result['held'], 1)
        self.assertIn('untracked_source_untracked_card', self.log_text())

    def test_rebase_in_progress_skips_the_whole_run(self):
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
        card = self.card('gone-card.md', 'gone.md', body)
        self.commit('add source and card', '.')
        git(self.root, 'rm', '-q', 'docs/gone.md')
        git(self.root, 'commit', '-q', '-m', 'delete source')
        git(self.root, 'checkout', '-q', 'topic')
        git(self.root, 'rebase', 'main', ok=(1,))
        self.assertTrue((self.root / '.git/rebase-merge').exists()
                        or (self.root / '.git/rebase-apply').exists())
        result = self.run_lifecycle()
        self.assertTrue(card.exists(), self.log_text())
        self.assertEqual(result['examined'], 0)
        self.assertIn(result.get('gitOperation'), ('rebase-merge', 'rebase-apply'))
        self.assertIn('in progress', self.log_text())
        git(self.root, 'rebase', '--abort')

    def test_lifecycle_never_runs_inside_a_linked_worktree(self):
        body = b'only in main checkout\n'
        self.source('main-only.md', body)
        self.commit('add source', 'docs/main-only.md')
        worktree = self.base / 'linked'
        git(self.root, 'worktree', 'add', '-q', '-b', 'side', str(worktree), 'HEAD~1')
        card = worktree / '.auto-context/wiki/concepts/card.md'
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(self.card('card.md', 'main-only.md', body).read_text())
        result = self.run_lifecycle(cwd=worktree)
        self.assertTrue(card.exists())
        self.assertEqual(result['route'], 'worktree')
        self.assertIn('linked git worktree', self.log_text())
        self.assertEqual(wiki_source_retire.retire_deleted_source(
            worktree, {}, self.selected, 'proj-docs', 'docs/main-only.md', 'a' * 64)['state'],
            'held_branch')

    def test_dry_run_reports_held_cards_without_writing(self):
        git(self.root, 'branch', 'feature')
        body = b'main-only\n'
        self.source('only-main.md', body)
        self.commit('add main-only source', 'docs/only-main.md')
        self.card('only-main-card.md', 'only-main.md', body)
        git(self.root, 'checkout', '-q', 'feature')
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual(report['counts']['missing'], 1)
        self.assertEqual(report['held'], 1)
        self.assertEqual(report['heldReasons'], {'source_on_other_branch': 1})
        self.assertEqual(report['guard']['wouldDelete'], 0)


class WorktreeRecallReadOnly(unittest.TestCase):
    """A linked worktree of a real-shaped selected install (temp HOME)."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'laya_selected_project', HERE / 'laya_selected_project.py')
        cls.selected_fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.selected_fixture)

    def setUp(self):
        selected = self.selected_fixture.SelectedProject('run')
        selected.setUp()
        self.addCleanup(selected.doCleanups)
        self.selected = selected
        self.project = selected.project
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        # Route record, selection marker and journal are untracked runtime
        # files; only settings and wiki are committed, as in a real project.
        (self.project / '.auto-context/wiki/note.md').write_text('# note\n')
        git(self.project, 'init', '-q')
        git(self.project, 'add', '.auto-context/settings.json', '.auto-context/wiki')
        git(self.project, 'commit', '-q', '-m', 'settings and wiki')
        self.host_route = self.selected_fixture.host_route
        self.response = self.selected_fixture.response

    def add_worktree(self, path):
        git(self.project, 'worktree', 'add', '-q', '-b', 'wt-' + path.name, str(path))
        return path

    def test_nested_and_sibling_worktrees_classify_read_only(self):
        main = self.host_route.select_route(str(self.project), action='recall')
        self.assertEqual(main['state'], 'candidate', main)
        for worktree in (self.add_worktree(self.project / '.claude/worktrees/agent-1'),
                         self.add_worktree(self.selected.home / 'sibling')):
            for cwd in (worktree, worktree / '.auto-context'):
                decision = self.host_route.select_route(str(cwd), action='recall')
                self.assertEqual(decision['state'], 'worktree', decision)
                self.assertEqual(decision['mainRoute'], main)
                self.assertEqual(decision['worktreeRoot'], str(worktree))
            payload = self.selected.home / ('payload-' + worktree.name + '.json')
            payload.write_text(json.dumps({'cwd': str(worktree), 'prompt': 'q'}))
            out = subprocess.run([sys.executable, '-B', str(CORE / 'laya_index_host_route.py'),
                                  'classify-hook', 'recall', str(payload), '/'],
                                 capture_output=True, text=True, env=os.environ.copy())
            self.assertEqual(out.stdout.strip(), 'worktree', out.stderr)

    def test_worktree_serves_main_generation_read_only_with_no_work(self):
        worktree = self.add_worktree(self.project / '.claude/worktrees/agent-1')
        decision = self.host_route.select_route(str(worktree), action='recall')
        response = self.response
        calls = []

        def fake_recall(host, served, payload, proof, recheck, *, degraded=False,
                        read_only_label='bundle_drift'):
            self.assertEqual(recheck(served), proof)
            calls.append({'state': served['state'], 'root': served['projectRoot'],
                          'degraded': degraded, 'label': read_only_label,
                          'readOnly': proof.get('readOnlyAllowed')})
            response._LEARNING_CAPTURE[:] = [('capture', 'would-run')]
            return response.host_payload(host, '[laya-index] cards')

        def forbidden(*_args, **_kwargs):
            raise AssertionError('a worktree hook must not record work')
        with mock.patch.object(response, '_serve_recall', fake_recall), \
                mock.patch.object(response, '_request_selected_sync', forbidden), \
                mock.patch.object(response, '_request_provider_start', forbidden), \
                mock.patch.object(response, '_request_stale_sync', forbidden), \
                mock.patch.object(response, '_start_owner', forbidden), \
                mock.patch.object(self.selected_fixture.discovery, 'record_grant', forbidden):
            out = response.serve_selected('recall', 'claude', decision,
                                          {'prompt': 'q', 'cwd': str(worktree)})
            self.assertEqual(response._LEARNING_CAPTURE, [])
            context = out['hookSpecificOutput']['additionalContext']
            self.assertTrue(context.startswith('[laya-index] read-only from the main checkout '
                                               + str(self.project)), context)
            self.assertTrue(context.endswith('[laya-index] cards'))
            self.assertEqual(calls, [{'state': 'candidate', 'root': str(self.project),
                                      'degraded': True, 'label': 'worktree',
                                      'readOnly': True}])
            notice = response.serve_selected('update-queued', 'claude', decision,
                                             {'hook_event_name': 'SessionStart',
                                              'cwd': str(worktree)})
            self.assertIn('linked git worktree',
                          notice['hookSpecificOutput']['additionalContext'])
            edit = {'hook_event_name': 'PostToolUse', 'tool_name': 'Edit',
                    'tool_input': {'file_path': str(worktree / 'a.md')}, 'cwd': str(worktree)}
            for action in ('posttool', 'compile', 'index', 'file-change-event'):
                self.assertEqual(response.serve_selected(action, 'claude', decision, edit), {})
            gate = response.serve_selected('gate', 'claude', decision,
                                           {**edit, 'hook_event_name': 'PreToolUse'})
            self.assertEqual(gate['hookSpecificOutput']['permissionDecision'], 'allow')

    def test_worktree_never_asks_for_setup(self):
        import setup_guard
        worktree = self.add_worktree(self.project / '.claude/worktrees/agent-1')
        state = setup_guard.status(str(worktree))
        self.assertEqual(state['status'], 'worktree_read_only', state)
        self.assertEqual(state['mainProject'], str(self.project))
        self.assertEqual(setup_guard.main(['check', str(worktree)]), 4)

    def test_unproven_identity_keeps_todays_classification(self):
        changed = self.add_worktree(self.selected.home / 'changed')
        settings = changed / '.auto-context/settings.json'
        raw = json.loads(settings.read_text())
        raw['name'] = 'other-project'  # derives other collection names
        settings.write_text(json.dumps(raw))
        self.assertNotEqual(self.host_route.select_route(str(changed))['state'], 'worktree')
        own = self.add_worktree(self.selected.home / 'own-route')
        (own / '.auto-context' / self.host_route.RECORD).write_text('{}')
        self.assertEqual(self.host_route.select_route(str(own))['state'], 'blocked')
        clone = self.selected.home / 'clone'
        git(self.selected.home, 'clone', '-q', str(self.project), str(clone))
        self.assertEqual(self.host_route.select_route(str(clone))['state'], 'legacy')


if __name__ == '__main__':
    unittest.main()
