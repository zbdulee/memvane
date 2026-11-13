#!/usr/bin/env python3
"""P8-1 compile-time observed source fingerprints against real temporary git repos.

The worker's single ``snapshot_bytes`` read yields both the compiler-owned
``sourceRevisions`` and the ``sourceFingerprints`` spec. The checkout binding is
the caller-owned registration pair driven through the real
``laya_checkout_view`` capture/match API (fresh observations, no cache). Every
row is checked by the real memvane-db value validator, so the spec is the shape
the store accepts. No model, journal, DB or selected route: the extractor is a
counted fake and the route is the legacy one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'core'))

GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
for _key in [key for key in os.environ if key.startswith('GIT_')]:
    del os.environ[_key]
os.environ.update(GIT_ENV)

from memvane.wiki.lifecycle import git_branch_safety  # noqa: E402
from memvane.kernel import paths as git_paths  # noqa: E402
from memvane.store.blob import blob_values as laya_blob_values  # noqa: E402
from memvane.store.checkout import checkout_view as view  # noqa: E402
from memvane.wiki.compile import wiki_compile_worker as worker  # noqa: E402
from memvane.wiki.card import wiki_freshness  # noqa: E402
from memvane.wiki.workspace import wiki_selected_laya  # noqa: E402

REAL_RUN_GIT = git_branch_safety.run_git
REAL_SNAPSHOT = wiki_freshness.snapshot_bytes
REAL_COMPILE = worker.compile_candidate
SOURCE = 'docs/source.md'
SOURCE_BODY = '# Source\n\nDurable decision: cards cite source.\n'
DECOY = {'schema': 'model', 'fingerprints': [{'path': SOURCE, 'source_sha': '0' * 64}]}


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=str(root), check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


class Project:
    def __init__(self, base: Path, name: str, *, git_repo: bool):
        self.root = base / name
        self.docs = self.root / 'docs'
        self.wiki = self.root / '.auto-context' / 'wiki'
        self.docs.mkdir(parents=True)
        self.wiki.mkdir(parents=True)
        (self.root / '.auto-context' / 'compile').mkdir(parents=True)
        self.source = self.root / SOURCE
        self.source.write_text(SOURCE_BODY, encoding='utf-8')
        os.utime(self.source, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        (self.docs / 'other.md').write_text('# Other\n', encoding='utf-8')
        self.config = {
            'indexing': True,
            'collections': ['proj-docs', 'proj-wiki'],
            'collectionPaths': {'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki'},
            'collectionRoles': {'proj-docs': 'raw', 'proj-wiki': 'wiki'},
            'wikiPath': '.auto-context/wiki',
            'compile': {
                'mode': 'guarded', 'defaultStatus': 'generated',
                'triggers': ['post_tool_source', 'manual'], 'maxSourceChars': 12000,
                'extractor': {'backends': {'claude': ['fake-extractor']}, 'timeout': 30},
            },
        }
        (self.root / '.auto-context' / 'settings.json').write_text(
            json.dumps(self.config), encoding='utf-8')
        if git_repo:
            git(self.root, 'init', '-q', '-b', 'main')
            git(self.root, 'add', '.auto-context/settings.json')
            git(self.root, 'commit', '-q', '-m', 'init')

    def binding(self):
        repository = view.register_repository(str(self.root))
        worktree = view.register_worktree(str(self.root), repository)
        self.captures = []

        def capture():
            value = view.capture_view(str(self.root), repository, worktree, fresh=True)
            self.captures.append(value)
            return value
        return SimpleNamespace(
            capture=capture,
            matches=lambda value: view.capture_matches(value, repository, worktree, fresh=True))

    def job(self):
        return {'ts': '2026-11-06T00:00:00Z', 'trigger': 'post_tool_source', 'engine': 'claude',
                'cwd': str(self.root),
                'source': {'kind': 'file', 'path': SOURCE, 'collection': 'proj-docs'}}

    def rows(self):
        path = worker.candidate_path(self.root, self.config['compile'])
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


class FingerprintChecks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='wiki-fingerprint-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.extractor_calls = 0
        self.candidates = []
        self.model_extra = {}
        patches = [
            mock.patch.dict(os.environ, {'QMD_DIRTY_QUEUE': str(self.base / 'dirty-queue')}),
            mock.patch.object(wiki_selected_laya, 'selected', return_value=None),
            mock.patch.object(wiki_selected_laya, 'recheck'),
            mock.patch.object(worker, 'run_extractor', side_effect=self.fake_extractor),
            mock.patch.object(worker, 'gather_similar_pages', return_value=[]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def fake_extractor(self, argv, payload, timeout, root):
        self.extractor_calls += 1
        return {'candidates': [{'title': 'Source decision', 'summary': 'Cards cite source.',
                                'confidence': 'high',
                                **self.model_extra}]}, None, 0

    def fake_compile(self, root, candidate, **_):
        self.candidates.append(json.loads(json.dumps(candidate)))
        return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions'],
                'targetPath': '.auto-context/wiki/concepts/source-decision.md'}

    def run_job(self, project, checkout, *, compile_fn=None):
        with mock.patch.object(worker, 'compile_candidate',
                               side_effect=compile_fn or self.fake_compile):
            return worker.process_job(project.root, project.config, project.config['compile'],
                                      project.job(), checkout=checkout)

    def spec_of(self, project, checkout):
        self.assertEqual(self.run_job(project, checkout), (True, False, []))
        candidate = self.candidates[-1]
        spec = candidate['sourceFingerprints']
        capture = project.captures[0]
        self.assertEqual(spec['schema'], worker.SOURCE_FINGERPRINTS_SCHEMA)
        self.assertEqual((spec['viewId'], spec['checkoutDigest'], spec['gitObjectFormat']),
                         (capture['viewId'], capture['checkoutDigest'],
                          capture['gitObjectFormat']))
        self.assertEqual(len(spec['fingerprints']), 1)
        row = spec['fingerprints'][0]
        revisions = candidate['sourceRevisions']
        self.assertEqual(len(revisions), 1)
        # Same read: path, sha and size equal the frontmatter provenance exactly.
        self.assertEqual((row['path'], row['source_sha'], row['byte_length']),
                         (revisions[0]['path'], revisions[0]['sha256'], revisions[0]['size']))
        self.assertEqual(set(row), laya_blob_values.FINGERPRINT_FIELDS)
        self.assertEqual(laya_blob_values.fingerprint(row, spec['gitObjectFormat']), row)
        self.assertEqual((row['worktree_id'], row['branch_key'], row['head_oid']),
                         (capture['worktreeId'], capture['branchKey'], capture['headOid']))
        self.assertEqual(row['binding_state'], 'observed')
        return row

    # --- tracked state matrix -------------------------------------------------

    def test_tracked_committed_source(self):
        project = Project(self.base, 'tracked', git_repo=True)
        git(project.root, 'add', SOURCE)
        git(project.root, 'commit', '-q', '-m', 'source')
        row = self.spec_of(project, project.binding())
        self.assertEqual(row['tracked_state'], 'tracked')
        self.assertEqual(row['branch_key'], 'refs/heads/main')
        self.assertEqual(row['head_oid'], git(project.root, 'rev-parse', 'HEAD'))

    def test_untracked_source(self):
        project = Project(self.base, 'untracked', git_repo=True)
        row = self.spec_of(project, project.binding())
        self.assertEqual(row['tracked_state'], 'untracked')
        self.assertEqual(row['head_oid'], git(project.root, 'rev-parse', 'HEAD'))

    def test_staged_new_source_is_tracked(self):
        project = Project(self.base, 'staged', git_repo=True)
        git(project.root, 'add', SOURCE)
        self.assertEqual(git(project.root, 'ls-tree', '--name-only', 'HEAD', '--', SOURCE), '')
        row = self.spec_of(project, project.binding())
        self.assertEqual(row['tracked_state'], 'tracked')

    def test_unborn_branch_has_no_head(self):
        project = Project(self.base, 'unborn', git_repo=False)
        git(project.root, 'init', '-q', '-b', 'main')
        row = self.spec_of(project, project.binding())
        self.assertEqual((row['branch_key'], row['head_oid'], row['tracked_state']),
                         ('refs/heads/main', None, 'untracked'))
        git(project.root, 'add', SOURCE)
        self.assertEqual(self.spec_of(project, project.binding())['tracked_state'], 'tracked')

    def test_detached_head(self):
        project = Project(self.base, 'detached', git_repo=True)
        git(project.root, 'add', SOURCE)
        git(project.root, 'commit', '-q', '-m', 'source')
        git(project.root, 'checkout', '-q', '--detach')
        head = git(project.root, 'rev-parse', 'HEAD')
        row = self.spec_of(project, project.binding())
        self.assertEqual((row['branch_key'], row['head_oid'], row['tracked_state']),
                         ('detached:' + head, head, 'tracked'))

    def test_non_git_project(self):
        project = Project(self.base, 'plain', git_repo=False)
        with mock.patch.object(git_branch_safety, 'run_git',
                               side_effect=AssertionError('non-git runs no git')) as run_mock, \
             mock.patch.object(git_paths, 'run_git', new=run_mock):
            row = self.spec_of(project, project.binding())
        self.assertEqual((row['branch_key'], row['head_oid'], row['tracked_state']),
                         ('non-git', None, 'untracked'))
        self.assertIsNone(self.candidates[-1]['sourceFingerprints']['gitObjectFormat'])

    def test_git_failure_is_unknown(self):
        project = Project(self.base, 'failure', git_repo=True)
        git(project.root, 'add', SOURCE)
        git(project.root, 'commit', '-q', '-m', 'source')
        with mock.patch.object(git_branch_safety, 'run_git',
                               side_effect=git_branch_safety.GitUnavailable('git_exit_128')) as run_mock, \
             mock.patch.object(git_paths, 'run_git', new=run_mock):
            row = self.spec_of(project, project.binding())
        self.assertEqual(row['tracked_state'], 'unknown')
        self.assertEqual(self.extractor_calls, 1)

    def test_git_timeout_is_unknown(self):
        project = Project(self.base, 'timeout', git_repo=True)
        git(project.root, 'add', SOURCE)
        git(project.root, 'commit', '-q', '-m', 'source')
        calls = []

        def slow(args, cwd, **kwargs):
            calls.append(args)
            kwargs['timeout'] = 1e-9
            kwargs['deadline'] = None
            return REAL_RUN_GIT(args, cwd, **kwargs)
        with mock.patch.object(git_branch_safety, 'run_git', side_effect=slow) as run_mock, \
             mock.patch.object(git_paths, 'run_git', new=run_mock):
            row = self.spec_of(project, project.binding())
        self.assertEqual(row['tracked_state'], 'unknown')
        self.assertEqual(len(calls), 1, 'the first timed-out call ends the lookup')

    def test_git_calls_do_not_scale_with_paths(self):
        project = Project(self.base, 'bounded', git_repo=True)
        git(project.root, 'add', SOURCE)
        git(project.root, 'commit', '-q', '-m', 'source')
        head = git(project.root, 'rev-parse', 'HEAD')
        paths = [SOURCE] + [f'docs/missing-{index}.md' for index in range(
            git_branch_safety.MAX_PATHSPECS_PER_CALL - 1)]
        calls = []

        def counted(args, cwd, **kwargs):
            calls.append(args[0])
            return REAL_RUN_GIT(args, cwd, **kwargs)
        with mock.patch.object(git_branch_safety, 'run_git', side_effect=counted) as run_mock, \
             mock.patch.object(git_paths, 'run_git', new=run_mock):
            states = git_branch_safety.tracked_states(project.root, paths, head)
        self.assertEqual(states[SOURCE], 'tracked')
        self.assertEqual({states[path] for path in paths[1:]}, {'untracked'})
        # rev-parse + one index call + one HEAD-tree call for a full pathspec chunk.
        self.assertEqual(calls, ['rev-parse', 'ls-files', 'ls-tree'])
        calls.clear()
        # Two paths over one chunk: two index calls; the HEAD tree is asked only
        # for the paths the index did not answer (201 here), so also two calls.
        more = paths + ['docs/one-more.md', 'docs/two-more.md']
        with mock.patch.object(git_branch_safety, 'run_git', side_effect=counted) as run_mock, \
             mock.patch.object(git_paths, 'run_git', new=run_mock):
            git_branch_safety.tracked_states(project.root, more, head)
        self.assertEqual(calls, ['rev-parse', 'ls-files', 'ls-files', 'ls-tree', 'ls-tree'])

    # --- checkout changes during the read hold the job before paid extraction ----

    def assert_held(self, project, checkout, reason):
        self.assertEqual(self.run_job(project, checkout), (False, True, []))
        self.assertEqual(self.extractor_calls, 0)
        self.assertEqual(self.candidates, [])
        self.assertEqual([(row['action'], row['reason']) for row in project.rows()],
                         [('deferred', reason)])

    def changing_snapshot(self, project, *args):
        def snapshot(path):
            git(project.root, *args)
            return REAL_SNAPSHOT(path)
        return mock.patch.object(wiki_freshness, 'snapshot_bytes', side_effect=snapshot)

    def test_branch_switch_during_read_holds(self):
        project = Project(self.base, 'switch', git_repo=True)
        checkout = project.binding()
        with self.changing_snapshot(project, 'switch', '-q', '-c', 'other'):
            self.assert_held(project, checkout, 'checkout_changed_during_read')

    def test_index_change_during_read_holds(self):
        project = Project(self.base, 'index', git_repo=True)
        checkout = project.binding()
        with self.changing_snapshot(project, 'add', SOURCE):
            self.assert_held(project, checkout, 'checkout_changed_during_read')

    def test_head_move_during_read_holds(self):
        project = Project(self.base, 'commit', git_repo=True)
        checkout = project.binding()
        with self.changing_snapshot(project, 'commit', '-q', '--allow-empty', '-m', 'move'):
            self.assert_held(project, checkout, 'checkout_changed_during_read')

    def test_unobservable_checkout_holds(self):
        project = Project(self.base, 'unobservable', git_repo=True)
        checkout = project.binding()
        shutil.rmtree(project.root / '.git')
        self.assert_held(project, checkout, 'checkout_unavailable')

    # --- model fields never become fingerprints ----------------------------------

    def test_model_fingerprints_are_replaced(self):
        project = Project(self.base, 'decoy', git_repo=True)
        self.model_extra = {'sourceFingerprints': DECOY}
        row = self.spec_of(project, project.binding())
        self.assertNotEqual(self.candidates[-1]['sourceFingerprints'], DECOY)
        self.assertEqual(row['scan_id'], self.candidates[-1]['sourceFingerprints']
                         ['fingerprints'][0]['scan_id'])

    def test_model_fingerprints_are_dropped_without_checkout(self):
        project = Project(self.base, 'decoy-legacy', git_repo=False)
        self.model_extra = {'sourceFingerprints': DECOY}
        self.assertEqual(self.run_job(project, None), (True, False, []))
        self.assertNotIn('sourceFingerprints', self.candidates[-1])

    def test_batch_passes_selected_checkout_to_the_real_job(self):
        from memvane.wiki.intake import wiki_backfill as laya_wiki_backfill
        project = Project(self.base, 'batch-selected', git_repo=True)
        queue = project.root / '.auto-context' / 'compile' / 'source-queue.jsonl'
        queue.write_text(json.dumps(project.job()) + '\n', encoding='utf8')
        checkout = project.binding()
        with mock.patch.object(wiki_selected_laya, 'schema3_checkout', return_value=checkout), \
                mock.patch.object(laya_wiki_backfill, 'reconcile_discards'), \
                mock.patch.object(laya_wiki_backfill, 'settle_job'), \
                mock.patch.object(worker, 'recover_pending_refreshes'), \
                mock.patch.object(worker, '_run_verify_pass'), \
                mock.patch.object(worker, 'compile_candidate', side_effect=self.fake_compile):
            self.assertEqual(worker._main_locked(SimpleNamespace(json=False, flush_all=True),
                             project.root, project.config, project.config['compile'], queue,
                             ('selected', 'proof')), 0)
        self.assertEqual(len(self.candidates), 1)
        row = self.candidates[0]['sourceFingerprints']['fingerprints'][0]
        self.assertEqual((row['path'], row['source_sha']),
                         (SOURCE, self.candidates[0]['sourceRevisions'][0]['sha256']))

    def test_batch_reader_error_defers_without_paid_extraction(self):
        from memvane.wiki.intake import wiki_backfill as laya_wiki_backfill
        project = Project(self.base, 'batch-reader-busy', git_repo=True)
        queue = project.root / '.auto-context' / 'compile' / 'source-queue.jsonl'
        queue.write_text(json.dumps(project.job()) + '\n', encoding='utf8')
        with mock.patch.object(wiki_selected_laya, 'schema3_checkout',
                               side_effect=sqlite3.OperationalError('fixture reader busy')), \
                mock.patch.object(laya_wiki_backfill, 'reconcile_discards'), \
                mock.patch.object(worker, 'recover_pending_refreshes'), \
                mock.patch.object(worker, '_run_verify_pass'):
            self.assertEqual(worker._main_locked(SimpleNamespace(json=False, flush_all=True),
                             project.root, project.config, project.config['compile'], queue,
                             ('selected', 'proof')), 0)
        self.assertEqual(self.extractor_calls, 0)
        self.assertTrue(queue.read_text(encoding='utf8').strip())
        self.assertEqual(project.rows()[-1]['reason'], 'checkout_unavailable')

    def test_schema3_writer_enqueues_observed_fingerprints(self):
        import contextlib
        import io
        from memvane.host import host_response as laya_host_response
        from memvane.wiki.workspace import wiki_activity_log
        from memvane.wiki.compile import wiki_compile
        project = Project(self.base, 'schema3-queue', git_repo=True)
        self.assertEqual(self.run_job(project, project.binding()), (True, False, []))
        candidate = self.candidates[-1]
        decision = {'recallSettings': {'skipPaths': []}, 'collections': [
            {'name': 'proj-docs', 'role': 'raw', 'sourceRoot': str(project.docs)},
            {'name': 'proj-wiki', 'role': 'wiki', 'sourceRoot': str(project.wiki)}]}
        with mock.patch.object(wiki_selected_laya, 'selected', return_value=(decision, {})), \
                mock.patch.object(wiki_selected_laya, 'request_sync'), \
                mock.patch.object(wiki_selected_laya, 'wiki_matches', return_value=[]), \
                mock.patch.object(wiki_activity_log, 'append'), \
                mock.patch.object(laya_host_response, '_selected_store_format', return_value='schema3'), \
                contextlib.redirect_stdout(io.StringIO()):
            code = wiki_compile._compile_locked(project.root, project.config,
                project.config['compile'], 'guarded', project.wiki, candidate, False)
        self.assertEqual(code, 0)
        queue = project.root / '.auto-context' / 'compile' / 'verify-queue.jsonl'
        job = json.loads(queue.read_text(encoding='utf8').strip())
        spec = job['sourceFingerprints']
        self.assertEqual(spec['viewId'], candidate['sourceFingerprints']['viewId'])
        self.assertEqual(spec['fingerprints'], candidate['sourceFingerprints']['fingerprints'])
        self.assertEqual(spec['scanId'], spec['fingerprints'][0]['scan_id'])
        self.assertEqual(spec['observedNs'], spec['fingerprints'][0]['observed_ns'])

    def test_writer_reader_error_preserves_verify_queue(self):
        import contextlib
        import io
        from memvane.host import host_response as laya_host_response
        from memvane.wiki.workspace import wiki_activity_log
        from memvane.wiki.compile import wiki_compile
        project = Project(self.base, 'writer-reader-busy', git_repo=True)
        self.assertEqual(self.run_job(project, project.binding()), (True, False, []))
        decision = {'recallSettings': {'skipPaths': []}, 'collections': [
            {'name': 'proj-docs', 'role': 'raw', 'sourceRoot': str(project.docs)},
            {'name': 'proj-wiki', 'role': 'wiki', 'sourceRoot': str(project.wiki)}]}
        with mock.patch.object(wiki_selected_laya, 'selected', return_value=(decision, {})), \
                mock.patch.object(wiki_selected_laya, 'request_sync'), \
                mock.patch.object(wiki_selected_laya, 'wiki_matches', return_value=[]), \
                mock.patch.object(wiki_activity_log, 'append'), \
                mock.patch.object(laya_host_response, '_selected_store_format',
                                  side_effect=sqlite3.OperationalError('fixture reader busy')), \
                contextlib.redirect_stdout(io.StringIO()):
            code = wiki_compile._compile_locked(project.root, project.config,
                project.config['compile'], 'guarded', project.wiki, self.candidates[-1], False)
        self.assertEqual(code, 0)
        queue = project.root / '.auto-context' / 'compile' / 'verify-queue.jsonl'
        job = json.loads(queue.read_text(encoding='utf8').strip())
        self.assertNotIn('sourceFingerprints', job)

    def test_scan_id_is_per_observation(self):
        project = Project(self.base, 'scan', git_repo=True)
        checkout = project.binding()
        first = self.spec_of(project, checkout)
        second = self.spec_of(project, checkout)
        self.assertNotEqual(first['scan_id'], second['scan_id'])
        self.assertEqual(first['source_sha'], second['source_sha'])

    # --- existing writer behaviour is unchanged ----------------------------------

    def test_card_and_verify_job_unchanged_by_fingerprints(self):
        plain = Project(self.base, 'plain-writer', git_repo=False)
        bound = Project(self.base, 'bound-writer', git_repo=True)
        handed = []

        def spy(root, candidate, **kwargs):
            handed.append(json.loads(json.dumps(candidate)))
            return REAL_COMPILE(root, candidate, **kwargs)
        self.assertEqual(self.run_job(plain, None, compile_fn=spy)[:2], (True, False))
        self.assertEqual(self.run_job(bound, bound.binding(), compile_fn=spy)[:2], (True, False))
        self.assertNotIn('sourceFingerprints', handed[0])
        self.assertIn('sourceFingerprints', handed[1])
        self.assertEqual(handed[0], {k: v for k, v in handed[1].items()
                                     if k != 'sourceFingerprints'})
        cards = []
        for project in (plain, bound):
            found = sorted(project.wiki.rglob('*.md'))
            self.assertEqual(len(found), 1, project.root)
            cards.append((found[0].relative_to(project.root).as_posix(), found[0].read_bytes()))
        self.assertEqual(cards[0], cards[1])
        queues = []
        for project in (plain, bound):
            queue = project.root / '.auto-context' / 'compile' / 'verify-queue.jsonl'
            rows = [json.loads(line) for line in queue.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows), 1)
            rows[0].pop('ts')
            queues.append(rows[0])
        self.assertEqual(queues[0], queues[1])



if __name__ == '__main__':
    unittest.main()
