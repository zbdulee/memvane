#!/usr/bin/env python3
"""schema3 source-deletion proof against real temporary git repositories.

A missing source retires its card only when the deletion is a committed
deletion in the same worktree's lineage (design §7.1): the fingerprint is
``observed`` in this worktree, its ``head_oid`` is a known ancestor of HEAD,
the path was committed at that head, a complete scan and the disk both miss
it, HEAD and the index do not list it, no history operation runs, and a final
re-check still agrees. Every other shape holds. Each case also bounds the git
calls by count.

Every repository is a temp directory. Git runs with no global or system
config so signing, hooks and default-branch settings of the host never apply.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
CORE = HERE.parents[1] / 'core'
sys.path.insert(0, str(CORE))
import git_branch_safety as gbs  # noqa: E402

GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'fixture',
           'GIT_AUTHOR_EMAIL': 'fixture@example.invalid', 'GIT_COMMITTER_NAME': 'fixture',
           'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
SOURCE = 'docs/a.md'
# Fixed setup cost of one proof session: rev-parse facts, HEAD, HEAD tree,
# symbolic-ref. A retiring proof adds index, ancestry, recorded-commit path and
# a two-call re-check.
SETUP_CALLS = 4
RETIRE_CALLS = 5


def git(cwd, *args, ok=(0,)):
    result = subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                             '-c', 'init.defaultBranch=main', '-c', 'protocol.file.allow=always',
                             *args],
                            cwd=str(cwd), env={**os.environ, **GIT_ENV},
                            capture_output=True, text=True, check=False)
    if result.returncode not in ok:
        raise AssertionError(f'git {args} failed: {result.stderr}')
    return result


def head(cwd):
    return git(cwd, 'rev-parse', 'HEAD').stdout.strip()


def identity(path):
    path = Path(path).resolve()
    info = os.stat(path)
    return {'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino}


def registration(root):
    root = Path(root).resolve()
    top = Path(git(root, 'rev-parse', '--show-toplevel').stdout.strip())
    admin = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-dir').stdout.strip())
    return {'worktreeId': uuid.uuid4().hex, 'project': identity(root),
            'checkout': identity(top), 'gitDir': identity(admin)}


class Repo(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='git-deletion-proof-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'project'
        (self.root / 'docs').mkdir(parents=True)
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        git(self.root, 'init', '-q')
        self.write(SOURCE, b'alpha\n')
        self.write('docs/keep.md', b'keep\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'base')
        self.worktree = registration(self.root)

    def write(self, rel, data, root=None):
        path = (root or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def fingerprint(self, *, root=None, worktree=None, path=SOURCE, head_oid=None,
                    tracked='tracked', binding='observed', branch='refs/heads/main'):
        root = root or self.root
        worktree = worktree or self.worktree
        data = (root / path).read_bytes() if (root / path).exists() else b''
        return {'fingerprint_id': hashlib.sha256(
                    json.dumps([worktree['worktreeId'], path, head_oid]).encode()).hexdigest(),
                'worktree_id': worktree['worktreeId'], 'branch_key': branch, 'path': path,
                'source_sha': hashlib.sha256(data).hexdigest(), 'byte_length': len(data),
                'head_oid': head(root) if head_oid is None else head_oid,
                'tracked_state': tracked, 'binding_state': binding,
                'scan_id': 'scan-observed', 'observed_ns': 1}

    def scan(self, *, root=None, present=(), complete=True, scan_id='scan-current'):
        return {'scan_id': scan_id, 'complete': complete,
                'root': identity(root or self.root), 'present_paths': frozenset(present)}

    def commit_delete(self, root=None, path=SOURCE):
        git(root or self.root, 'rm', '-q', path)
        git(root or self.root, 'commit', '-qm', 'delete ' + path)

    def prove(self, fingerprint, scan=None, *, root=None, worktree=None, **kwargs):
        return gbs.prove_source_deletion(
            root or self.root, worktree or self.worktree, fingerprint,
            scan or self.scan(root=root), view_id='view-1', base_gen='gen-1', **kwargs)

    def counted(self, fn):
        with mock.patch.object(gbs, 'run_git', wraps=gbs.run_git) as spy:
            value = fn()
        return value, spy.call_count

    def assert_hold(self, spec, reason):
        self.assertEqual((spec['decision'], spec['reason']), ('hold', reason), spec['proof_json'])


class RetireAndReceiptShape(Repo):

    def test_committed_same_lineage_deletion_retires_with_full_receipt(self):
        fp = self.fingerprint()
        recorded = fp['head_oid']
        self.commit_delete()
        current = head(self.root)
        spec, calls = self.counted(lambda: self.prove(fp, now_ns=7))
        self.assertEqual((spec['decision'], spec['reason']),
                         ('retire', 'committed_deletion_in_lineage'), spec['proof_json'])
        self.assertEqual(calls, SETUP_CALLS + RETIRE_CALLS)
        self.assertEqual(set(spec), {'receipt_id', 'view_id', 'base_gen', 'path', 'fingerprint_id',
                                     'observed_head', 'proof_json', 'decision', 'reason',
                                     'created_ns'})
        self.assertEqual((spec['view_id'], spec['base_gen'], spec['path'], spec['fingerprint_id'],
                          spec['observed_head'], spec['created_ns']),
                         ('view-1', 'gen-1', SOURCE, fp['fingerprint_id'], current, 7))
        proof = json.loads(spec['proof_json'])
        self.assertEqual(spec['proof_json'],
                         json.dumps(proof, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
        self.assertEqual((proof['recordedCommit'], proof['currentCommit']), (recorded, current))
        self.assertEqual((proof['ancestry'], proof['recordedPath'], proof['headPath'],
                          proof['indexPath'], proof['diskPath'], proof['operation'],
                          proof['history'], proof['recheck']),
                         ('ancestor', 'present', 'absent', 'absent', 'absent', 'absent',
                          'complete', 'passed'))
        self.assertEqual(proof['worktree']['verdict'], 'same')
        self.assertEqual(proof['worktree']['worktreeId'], self.worktree['worktreeId'])
        self.assertEqual(proof['repo']['commonDir']['path'], str(self.root / '.git'))
        self.assertEqual((proof['scan']['scanId'], proof['scan']['complete'], proof['scan']['root']),
                         ('scan-current', True, 'same'))
        self.assertNotIn('alpha', spec['proof_json'])
        # Same inputs give the same receipt id; a crash re-run is idempotent.
        again = self.prove(fp, now_ns=8)
        self.assertEqual(again['receipt_id'], spec['receipt_id'])

    def test_checkout_view_registration_is_accepted_as_is(self):
        import laya_checkout_view as checkout_view
        repository = checkout_view.register_repository(self.root)
        worktree = checkout_view.register_worktree(self.root, repository)
        fp = self.fingerprint(worktree=worktree)
        self.commit_delete()
        spec = self.prove(fp, {**self.scan(), 'root': worktree['project']}, worktree=worktree)
        self.assertEqual(spec['decision'], 'retire', spec['proof_json'])

    def test_one_session_bounds_calls_and_caches_ancestry(self):
        names = [f'docs/n{index}.md' for index in range(6)]
        for name in names:
            self.write(name, name.encode())
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'more')
        fps = [self.fingerprint(path=name) for name in names]
        git(self.root, 'rm', '-q', *names)
        git(self.root, 'commit', '-qm', 'drop')

        def run():
            prover = gbs.DeletionProof.open(self.root, self.worktree)
            return [prover.prove(fp, self.scan(), view_id='v', base_gen='g') for fp in fps]
        specs, calls = self.counted(run)
        self.assertEqual({spec['decision'] for spec in specs}, {'retire'})
        # Ancestry of the shared recorded head is asked once.
        self.assertEqual(calls, SETUP_CALLS + 1 + len(fps) * (RETIRE_CALLS - 1))

    def test_linked_worktree_retires_its_own_committed_deletion(self):
        linked = self.base / 'linked'
        git(self.root, 'worktree', 'add', '-q', '-b', 'side', str(linked))
        mine = registration(linked)
        fp = self.fingerprint(root=linked, worktree=mine, branch='refs/heads/side')
        self.commit_delete(root=linked)
        spec = self.prove(fp, root=linked, worktree=mine)
        self.assertEqual(spec['decision'], 'retire', spec['proof_json'])

    def test_nested_project_maps_project_paths_to_repository_paths(self):
        project = self.root / 'sub'
        self.write('sub/docs/x.md', b'x\n')
        self.write('sub/keep.md', b'keep\n')  # git rm prunes empty parents
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'nested')
        mine = {**self.worktree, 'project': identity(project)}
        fp = self.fingerprint(root=project, worktree=mine, path='docs/x.md')
        git(self.root, 'rm', '-q', 'sub/docs/x.md')
        git(self.root, 'commit', '-qm', 'drop nested')
        spec = self.prove(fp, self.scan(root=project), root=project, worktree=mine)
        self.assertEqual(spec['decision'], 'retire', spec['proof_json'])


class HoldMatrix(Repo):

    def test_missing_or_unbound_fingerprint_holds_without_git(self):
        self.commit_delete()
        for fp, reason in ((None, 'fingerprint_missing'),
                           (self.fingerprint(binding='legacy_unbound'), 'legacy_unbound'),
                           ({'path': SOURCE}, 'fingerprint_invalid'),
                           (self.fingerprint(path='../escape.md'), 'fingerprint_invalid')):
            with self.subTest(reason=reason):
                spec, calls = self.counted(lambda: gbs.DeletionProof.open(
                    self.root, self.worktree).prove(fp, self.scan(), view_id='v', base_gen='g'))
                self.assert_hold(spec, reason)
                self.assertEqual(calls, SETUP_CALLS)

    def test_other_worktree_observation_holds(self):
        other = registration(self.root)  # same checkout, other registration
        fp = self.fingerprint(worktree=other)
        self.commit_delete()
        spec, calls = self.counted(lambda: self.prove(fp))
        self.assert_hold(spec, 'other_worktree')
        self.assertEqual(calls, SETUP_CALLS)
        self.assertEqual(json.loads(spec['proof_json'])['worktree']['verdict'], 'other')

    def test_main_fingerprint_evaluated_in_linked_worktree_holds(self):
        fp = self.fingerprint()
        linked = self.base / 'linked'
        git(self.root, 'worktree', 'add', '-q', '-b', 'side', str(linked))
        git(linked, 'rm', '-q', SOURCE)
        git(linked, 'commit', '-qm', 'drop on side')
        mine = registration(linked)
        spec = self.prove({**fp, 'branch_key': 'refs/heads/side'}, root=linked, worktree=mine)
        self.assert_hold(spec, 'other_worktree')
        # A stale registration of the same id does not prove the checkout either.
        spec = self.prove(fp, root=linked, worktree=self.worktree)
        self.assert_hold(spec, 'worktree_identity_changed')

    def test_branch_switch_holds(self):
        fp = self.fingerprint()
        git(self.root, 'checkout', '-qb', 'feature')
        self.commit_delete()
        self.assert_hold(self.prove(fp), 'branch_changed')

    def test_force_updated_branch_of_the_same_name_holds(self):
        fp = self.fingerprint()
        git(self.root, 'checkout', '-q', '--orphan', 'other')
        git(self.root, 'rm', '-rqf', '.')
        self.write('docs/keep.md', b'other lineage\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'other lineage')
        git(self.root, 'branch', '-f', 'main', 'other')
        git(self.root, 'checkout', '-q', 'main')
        spec = self.prove(fp)
        self.assert_hold(spec, 'recorded_commit_not_ancestor')
        self.assertEqual(json.loads(spec['proof_json'])['ancestry'], 'not_ancestor')

    def test_unknown_null_and_malformed_recorded_commit_hold(self):
        self.commit_delete()
        for oid, reason, ancestry in (('0' * 39 + '1', 'recorded_commit_unknown', 'unknown'),
                                      ('', 'recorded_commit_invalid', 'not_checked'),
                                      ('ABC', 'recorded_commit_invalid', 'not_checked')):
            with self.subTest(oid=oid):
                spec = self.prove(self.fingerprint(head_oid=oid))
                self.assert_hold(spec, reason)
                self.assertEqual(json.loads(spec['proof_json'])['ancestry'], ancestry)
        fp = self.fingerprint()
        fp['head_oid'] = None
        self.assert_hold(self.prove(fp), 'recorded_commit_null')

    def test_shallow_clone_holds(self):
        fp_head = head(self.root)
        self.write('docs/b.md', b'b\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'second')
        clone = self.base / 'clone'
        git(self.base, 'clone', '-q', '--depth', '1', 'file://' + str(self.root), str(clone))
        self.assertTrue((clone / '.git/shallow').exists())
        mine = registration(clone)
        fp = self.fingerprint(root=clone, worktree=mine, path='docs/b.md')
        git(clone, 'rm', '-q', 'docs/b.md')
        git(clone, 'commit', '-qm', 'drop b')
        spec = self.prove(fp, root=clone, worktree=mine)
        self.assert_hold(spec, 'shallow_repository')
        self.assertEqual(json.loads(spec['proof_json'])['history'], 'shallow')
        self.assertNotEqual(fp_head, fp['head_oid'])

    def test_grafted_history_holds(self):
        fp = self.fingerprint()
        self.commit_delete()
        (self.root / '.git/info').mkdir(exist_ok=True)
        (self.root / '.git/info/grafts').write_text(head(self.root) + '\n')
        self.assert_hold(self.prove(fp), 'grafted_history')

    def test_disk_only_deletion_of_tracked_file_holds(self):
        fp = self.fingerprint()
        (self.root / SOURCE).unlink()
        self.assert_hold(self.prove(fp), 'path_in_head')

    def test_staged_deletion_holds(self):
        fp = self.fingerprint()
        git(self.root, 'rm', '-q', SOURCE)
        spec = self.prove(fp)
        self.assert_hold(spec, 'path_in_head')
        self.assertEqual(json.loads(spec['proof_json'])['headPath'], 'present')

    def test_index_only_entries_hold(self):
        fp = self.fingerprint()
        blob = git(self.root, 'hash-object', '-w', SOURCE).stdout.strip()
        self.commit_delete()
        # Deleted in HEAD but staged again; the file is not on disk.
        git(self.root, 'update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + SOURCE)
        spec = self.prove(fp)
        self.assert_hold(spec, 'path_in_index')
        self.assertEqual(json.loads(spec['proof_json'])['indexPath'], 'present')
        # Removed from the index only: the file is still on disk.
        git(self.root, 'checkout', '-q', 'HEAD~1', '--', SOURCE)
        git(self.root, 'commit', '-qm', 'restore')
        fp = self.fingerprint()
        git(self.root, 'rm', '-q', '--cached', SOURCE)
        self.assert_hold(self.prove(fp), 'present_on_disk')

    def test_sparse_checkout_and_skip_worktree_hold(self):
        fp = self.fingerprint()
        git(self.root, 'update-index', '--skip-worktree', SOURCE)
        (self.root / SOURCE).unlink()
        self.assert_hold(self.prove(fp), 'path_in_head')
        git(self.root, 'update-index', '--no-skip-worktree', SOURCE)
        git(self.root, 'checkout', '-q', '--', SOURCE)
        self.write('other/x.md', b'x\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'other dir')
        fp = self.fingerprint()
        git(self.root, 'sparse-checkout', 'set', 'other')
        self.assertFalse((self.root / SOURCE).exists())
        self.assert_hold(self.prove(fp), 'path_in_head')

    def test_history_operations_in_progress_hold(self):
        fp = self.fingerprint()
        self.commit_delete()
        admin = self.root / '.git'
        for marker in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD',
                       'REVERT_HEAD', 'BISECT_LOG', 'sequencer', 'index.lock', 'HEAD.lock'):
            with self.subTest(marker=marker):
                target = admin / marker
                if marker.startswith(('rebase', 'sequencer')):
                    target.mkdir()
                else:
                    target.write_text(head(self.root) + '\n')
                try:
                    spec = self.prove(fp)
                    self.assert_hold(spec, 'git_operation_in_progress')
                    self.assertEqual(json.loads(spec['proof_json'])['operation'], marker)
                finally:
                    if target.is_dir():
                        target.rmdir()
                    else:
                        target.unlink()
        self.assertEqual(self.prove(fp)['decision'], 'retire')

    def test_real_merge_conflict_holds(self):
        fp = self.fingerprint()
        git(self.root, 'checkout', '-qb', 'topic')
        self.write('docs/keep.md', b'topic\n')
        git(self.root, 'commit', '-qam', 'topic')
        git(self.root, 'checkout', '-q', 'main')
        self.write('docs/keep.md', b'main\n')
        git(self.root, 'commit', '-qam', 'main edit')
        self.commit_delete()
        git(self.root, 'merge', 'topic', ok=(1,))
        self.assertTrue((self.root / '.git/MERGE_HEAD').exists())
        self.assert_hold(self.prove(fp), 'git_operation_in_progress')

    def test_untracked_card_unlink_holds(self):
        self.write('wiki/card.md', b'card\n')
        for tracked in ('untracked', 'tracked'):  # the DB value never replaces git's answer
            with self.subTest(tracked=tracked):
                if not (self.root / 'wiki/card.md').exists():
                    self.write('wiki/card.md', b'card\n')
                fp = self.fingerprint(path='wiki/card.md', tracked=tracked)
                (self.root / 'wiki/card.md').unlink()
                spec = self.prove(fp)
                self.assert_hold(spec, 'not_tracked_at_observation' if tracked == 'untracked'
                                 else 'not_committed_at_observed_head')

    def test_scan_shape_and_disk_disagreement_hold(self):
        fp = self.fingerprint()
        self.commit_delete()
        cases = (
            (self.scan(complete=False), 'scan_incomplete'),
            (self.scan(present=[SOURCE]), 'present_in_scan'),
            ({**self.scan(), 'root': identity(self.base)}, 'scan_root_changed'),
            ({'scan_id': 's'}, 'scan_invalid'),
        )
        for scan, reason in cases:
            with self.subTest(reason=reason):
                self.assert_hold(self.prove(fp, scan), reason)
        self.write(SOURCE, b'back\n')
        self.assert_hold(self.prove(fp), 'present_on_disk')

    def test_symlinked_parent_is_not_a_missing_path(self):
        fp = self.fingerprint()
        self.commit_delete()
        (self.root / 'docs').rename(self.root / 'real-docs')
        (self.root / 'docs').symlink_to(self.root / 'real-docs')
        self.assert_hold(self.prove(fp), 'path_unverifiable')

    def test_unreadable_parent_is_not_a_missing_path(self):
        if os.geteuid() == 0:
            self.skipTest('root ignores directory permissions')
        fp = self.fingerprint()
        self.commit_delete()
        (self.root / 'docs').chmod(0)
        self.addCleanup((self.root / 'docs').chmod, 0o755)
        self.assert_hold(self.prove(fp), 'path_unverifiable')

    def test_unborn_and_non_git_hold(self):
        fresh = self.base / 'fresh'
        (fresh / 'docs').mkdir(parents=True)
        git(fresh, 'init', '-q')
        mine = registration(fresh)
        fp = {**self.fingerprint(), 'worktree_id': mine['worktreeId']}
        spec = self.prove(fp, root=fresh, worktree=mine)
        self.assert_hold(spec, 'current_commit_unborn')
        self.assertIsNone(spec['observed_head'])
        plain = self.base / 'plain'
        plain.mkdir()
        spec = self.prove(fp, self.scan(root=plain), root=plain, worktree=mine)
        self.assert_hold(spec, 'not_git_repository')

    def test_exhausted_budget_holds(self):
        fp = self.fingerprint()
        self.commit_delete()
        self.assert_hold(self.prove(fp, budget=0), 'git_budget_exhausted')


class RecheckAndEnvironment(Repo):

    def test_public_recheck_detects_changes_after_retire_proof(self):
        fp = self.fingerprint()
        self.commit_delete()
        self.write('wiki/card.md', b'card v1\n')
        card = {'path': 'wiki/card.md', 'sha256': hashlib.sha256(b'card v1\n').hexdigest()}
        scan = self.scan()
        prover = gbs.DeletionProof.open(self.root, self.worktree)
        spec = prover.prove(fp, scan, view_id='view-1', base_gen='gen-1', expected_card=card)
        self.assertEqual(spec['decision'], 'retire')
        self.assertTrue(prover.recheck(fp, scan, card))
        self.write('wiki/card.md', b'card v2\n')
        self.assertFalse(prover.recheck(fp, scan, card))
        self.write('wiki/card.md', b'card v1\n')
        self.write(SOURCE, b'restored\n')
        self.assertFalse(prover.recheck(fp, scan, card))
        git(self.root, 'add', SOURCE)
        (self.root / SOURCE).unlink()
        self.assertFalse(prover.recheck(fp, scan, card))

    def test_prefetch_uses_protected_git_environment(self):
        prover = gbs.DeletionProof.open(self.root, self.worktree)
        polluted = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'invalid',
                    'GIT_CONFIG_VALUE_0': 'value'}
        with mock.patch.dict(os.environ, polluted):
            prover.prefetch(['docs/keep.md', 'docs/missing.md'])
        self.assertTrue(prover._in_index('docs/keep.md'))
        self.assertFalse(prover._in_index('docs/missing.md'))

    def test_head_moving_before_the_final_recheck_holds(self):
        fp = self.fingerprint()
        self.commit_delete()
        real = gbs.run_git

        def moving(args, cwd, **kwargs):
            if 'ls-tree' in args and '--name-only' in args and '-r' not in args:
                # Last fact before the re-check: a new commit lands now.
                result = real(args, cwd, **kwargs)
                self.write('docs/late.md', b'late\n')
                git(self.root, 'add', '-A')
                git(self.root, 'commit', '-qm', 'late')
                return result
            return real(args, cwd, **kwargs)
        with mock.patch.object(gbs, 'run_git', side_effect=moving):
            spec = self.prove(fp)
        self.assert_hold(spec, 'recheck_failed')
        self.assertEqual(json.loads(spec['proof_json'])['recheck'], 'failed')

    def test_card_bytes_changing_before_the_recheck_hold(self):
        fp = self.fingerprint()
        self.commit_delete()
        self.write('wiki/card.md', b'card v1\n')
        sha = hashlib.sha256(b'card v1\n').hexdigest()
        ok = self.prove(fp, expected_card={'path': 'wiki/card.md', 'sha256': sha})
        self.assertEqual(ok['decision'], 'retire', ok['proof_json'])
        self.write('wiki/card.md', b'card v2\n')
        spec = self.prove(fp, expected_card={'path': 'wiki/card.md', 'sha256': sha})
        self.assert_hold(spec, 'recheck_failed')

    def test_polluted_git_environment_is_ignored(self):
        decoy = self.base / 'decoy'
        (decoy / 'docs').mkdir(parents=True)
        git(decoy, 'init', '-q')
        (decoy / SOURCE).write_bytes(b'decoy\n')
        git(decoy, 'add', '-A')
        git(decoy, 'commit', '-qm', 'decoy')
        fp = self.fingerprint()
        self.commit_delete()
        polluted = {'GIT_DIR': str(decoy / '.git'), 'GIT_WORK_TREE': str(decoy),
                    'GIT_INDEX_FILE': str(decoy / '.git/index'),
                    'GIT_OBJECT_DIRECTORY': str(self.base / 'missing-objects'),
                    'GIT_COMMON_DIR': str(decoy / '.git'),
                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.worktree',
                    'GIT_CONFIG_VALUE_0': str(decoy),
                    'GIT_CONFIG_PARAMETERS': "'core.bare'='true'",
                    'GIT_REPLACE_REF_BASE': 'refs/decoy/', 'GIT_GRAFT_FILE': str(decoy / 'g')}
        with mock.patch.dict(os.environ, polluted):
            spec = self.prove(fp)
        self.assertEqual((spec['decision'], spec['reason']),
                         ('retire', 'committed_deletion_in_lineage'), spec['proof_json'])

    def test_replace_refs_cannot_forge_ancestry(self):
        fp = self.fingerprint()
        git(self.root, 'checkout', '-q', '--orphan', 'other')
        git(self.root, 'rm', '-rqf', '.')
        self.write('docs/keep.md', b'other\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'other root')
        git(self.root, 'branch', '-f', 'main', 'other')
        git(self.root, 'checkout', '-q', 'main')
        # A replace ref grafting the recorded head under HEAD would fake lineage.
        git(self.root, 'replace', '--graft', head(self.root), fp['head_oid'])
        self.assertEqual(git(self.root, 'merge-base', '--is-ancestor', fp['head_oid'], 'HEAD',
                             ok=(0, 1)).returncode, 0)
        self.assert_hold(self.prove(fp), 'recorded_commit_not_ancestor')


if __name__ == '__main__':
    unittest.main(verbosity=1)
