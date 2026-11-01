#!/usr/bin/env python3
"""PR 1 checkout identity and view capture against real temporary git repos.

Pins only the public contract of ``core/laya_checkout_view.py``: caller-owned
repository/worktree registrations, ``capture_view`` and ``capture_matches``.
Registration field names, evidence layout and digest inputs stay free. Every
repository lives under one temp root; fixture git runs sequentially with no
global/system config and a finite timeout. A fake git on PATH is used only
for failure and budget cases.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'core'))
import onboarding_choice  # noqa: E402

MODULES_BEFORE = set(sys.modules)
try:
    import laya_checkout_view as view  # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name != 'laya_checkout_view':
        raise
    view = None

REAL_GIT = shutil.which('git')
HEX32 = re.compile(r'[0-9a-f]{32}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
CAPTURE_KEYS = {'projectRoot', 'repoId', 'worktreeId', 'viewId', 'branchKey', 'headOid',
                'gitObjectFormat', 'checkoutDigest', 'evidence'}
MARKER_FILES = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG',
                'index.lock', 'HEAD.lock')
MARKER_DIRS = ('rebase-merge', 'rebase-apply', 'sequencer')
ROUTE_PREFIXES = ('laya_index', 'laya_host', 'laya_journal', 'laya_resident', 'laya_runtime',
                  'laya_embed', 'wiki_', 'context_learning')
CLEAN_ENV = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
GIT_ENV = {**CLEAN_ENV, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


def leaves(value):
    if isinstance(value, (dict, list)):
        for item in (value.values() if isinstance(value, dict) else value):
            yield from leaves(item)
    else:
        yield value


def replaced(value, old, new):
    if isinstance(value, dict):
        return {key: replaced(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [replaced(item, old, new) for item in value]
    return new if type(value) is type(old) and value == old else value


def canon(path) -> str:
    return str(onboarding_choice.canonical(path))


def flip(digest: str) -> str:
    return digest[:-1] + ('0' if digest[-1] != '0' else '1')


class CheckoutCase(unittest.TestCase):
    def setUp(self):
        if view is None:
            self.fail('core/laya_checkout_view.py is absent: PR 1 checkout capture not implemented')
        temp = tempfile.TemporaryDirectory(prefix='laya-checkout-view-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.env = {**GIT_ENV, 'GIT_CEILING_DIRECTORIES': str(self.base)}
        clean = mock.patch.dict(os.environ, CLEAN_ENV, clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        self.root = self.repo('project')

    def git(self, cwd, *args, ok=(0,)):
        self.assertTrue(Path(cwd).resolve().is_relative_to(self.base), cwd)
        result = subprocess.run(
            [REAL_GIT, '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
             '-c', 'init.defaultBranch=main', '-c', 'protocol.file.allow=always', *args],
            cwd=str(cwd), env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30, check=False)
        if result.returncode not in ok:
            raise AssertionError(f'git {args} failed: {result.stderr}')
        return result

    def out(self, cwd, *args):
        return self.git(cwd, *args).stdout.strip()

    def repo(self, name, *, commit=True):
        root = self.base / name
        root.mkdir(parents=True)
        self.git(root, 'init', '-q')
        if commit:
            self.commit(root, 'a.txt', 'one\n')
        return root

    def commit(self, root, name, text):
        (root / name).write_text(text)
        self.git(root, 'add', '--', name)
        self.git(root, 'commit', '-q', '-m', 'edit ' + name)
        return self.out(root, 'rev-parse', 'HEAD')

    def bind(self, root):
        repository = view.register_repository(str(root))
        return repository, view.register_worktree(str(root), repository)

    def cap(self, root, repository, worktree, **kwargs):
        return view.capture_view(str(root), repository, worktree, **kwargs)

    def refused(self, fn, *args, reason=None, **kwargs):
        with self.assertRaises(view.CheckoutUnavailable) as caught:
            fn(*args, **kwargs)
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertTrue(isinstance(caught.exception.reason, str) and caught.exception.reason)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)

    def no_match(self, capture, repository, worktree, **kwargs):
        self.assertIs(view.capture_matches(capture, repository, worktree, **kwargs), False)

    def fake_git(self, body):
        bindir = self.base / 'fakebin'
        bindir.mkdir(exist_ok=True)
        (bindir / 'git').write_text('#!/bin/sh\n' + body)
        (bindir / 'git').chmod(0o755)
        return mock.patch.dict(os.environ, {'PATH': str(bindir) + os.pathsep + os.environ['PATH']})

    def assert_no_process(self, pidfile):
        pids = [int(item) for item in pidfile.read_text().split()] if pidfile.exists() else []
        self.assertTrue(pids, 'fake git was never invoked')
        deadline = time.monotonic() + 5
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)  # existence probe only
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail(f'fake git process {pid} outlived the refusal')
                time.sleep(0.05)


class Registration(CheckoutCase):
    def test_repository_record_binds_common_dir_and_is_new_each_call(self):
        repository = view.register_repository(str(self.root))
        self.assertEqual(json.loads(json.dumps(repository)), repository)
        self.assertRegex(repository['repoId'], HEX32)
        self.assertTrue(any('schema' in key.lower() for key in repository), repository)
        common, values = canon(self.root / '.git'), list(leaves(repository))
        for fact in (common, os.stat(common).st_ino, os.stat(common).st_dev, 'sha1'):
            self.assertIn(fact, values)
        self.assertNotEqual(view.register_repository(str(self.root))['repoId'],
                            repository['repoId'])

    def test_worktree_record_cross_binds_and_survives_persistence(self):
        repository, worktree = self.bind(self.root)
        self.assertEqual(json.loads(json.dumps(worktree)), worktree)
        self.assertRegex(worktree['worktreeId'], HEX32)
        self.assertEqual(worktree['repoId'], repository['repoId'])
        self.assertTrue(any('schema' in key.lower() for key in worktree), worktree)
        values = list(leaves(worktree))
        for fact in (canon(self.root), canon(self.root / '.git'), os.stat(self.root).st_ino):
            self.assertIn(fact, values)
        self.assertTrue([v for v in values if isinstance(v, str) and HEX64.match(v)],
                        'worktree registration carries a repository-binding digest')
        self.assertNotEqual(view.register_worktree(str(self.root), repository)['worktreeId'],
                            worktree['worktreeId'])
        first = self.cap(self.root, repository, worktree)
        stored = json.loads(json.dumps([repository, worktree]))
        self.assertEqual(self.cap(self.root, *stored)['viewId'], first['viewId'])
        self.assertIs(view.capture_matches(first, *stored), True)

    def test_tampered_and_cross_bound_records_are_refused(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        other = self.repo('other')
        other_repo, other_wt = self.bind(other)
        common = canon(self.root / '.git')
        ino = os.stat(common).st_ino
        bad_repos = [dict(repository, repoId='A' * 32), dict(repository, repoId='g' * 32),
                     dict(repository, repoId=repository['repoId'][:-1]),
                     dict(repository, repoId=7), dict(repository, unexpected=True),
                     {k: v for k, v in repository.items() if k != 'repoId'},
                     replaced(repository, ino, ino + 1),
                     replaced(repository, common, canon(other / '.git')),
                     *[dict(repository, **{k: 'bogus'}) for k in repository
                       if 'schema' in k.lower()], None, []]
        for bad in bad_repos:
            with self.subTest(repository=bad):
                self.assertNotEqual(bad, repository)
                self.refused(view.register_worktree, str(self.root), bad)
                self.refused(self.cap, self.root, bad, worktree)
                self.no_match(capture, bad, worktree)
        digest = next(v for v in leaves(worktree) if isinstance(v, str) and HEX64.match(v))
        root_ino = os.stat(self.root).st_ino
        bad_worktrees = [dict(worktree, worktreeId='A' * 32), dict(worktree, unexpected=1),
                         {k: v for k, v in worktree.items() if k != 'worktreeId'},
                         dict(worktree, repoId=other_repo['repoId']),
                         replaced(worktree, digest, flip(digest)),
                         replaced(worktree, root_ino, root_ino + 1), None]
        for bad in bad_worktrees:
            with self.subTest(worktree=bad):
                self.assertNotEqual(bad, worktree)
                self.refused(self.cap, self.root, repository, bad)
                self.no_match(capture, repository, bad)
        self.refused(self.cap, self.root, other_repo, worktree)
        self.refused(self.cap, self.root, repository, other_wt)
        self.refused(self.cap, other, repository, worktree)
        self.refused(view.register_worktree, str(other), repository)


class CaptureShape(CheckoutCase):
    def test_capture_fields_and_stable_repeat(self):
        repository, worktree = self.bind(self.root)
        first = self.cap(self.root, repository, worktree)
        self.assertLessEqual(CAPTURE_KEYS, set(first))
        self.assertEqual(json.loads(json.dumps(first)), first)
        self.assertEqual((first['projectRoot'], first['repoId'], first['worktreeId']),
                         (canon(self.root), repository['repoId'], worktree['worktreeId']))
        self.assertEqual((first['branchKey'], first['headOid'], first['gitObjectFormat']),
                         ('refs/heads/main', self.out(self.root, 'rev-parse', 'HEAD'), 'sha1'))
        self.assertRegex(first['checkoutDigest'], HEX64)
        self.assertIsInstance(first['evidence'], dict)
        self.assertTrue(isinstance(first['viewId'], str) and first['viewId'])
        again = self.cap(self.root, repository, worktree)
        for key in ('viewId', 'checkoutDigest', 'headOid', 'branchKey'):
            self.assertEqual(again[key], first[key], key)
        self.assertIs(view.capture_matches(first, repository, worktree), True)
        self.assertIs(view.capture_matches(json.loads(json.dumps(first)), repository,
                                           worktree), True)
        second_wt = view.register_worktree(str(self.root), repository)
        self.assertNotEqual(self.cap(self.root, repository, second_wt)['viewId'], first['viewId'])
        self.no_match(first, repository, second_wt)
        self.assertNotEqual(self.cap(self.root, *self.bind(self.root))['viewId'], first['viewId'])

    def test_tampered_capture_never_matches(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        variants = [dict(capture, viewId='v' + capture['viewId']),
                    dict(capture, branchKey='refs/heads/other'),
                    dict(capture, headOid='0' * 40), dict(capture, headOid=None),
                    dict(capture, checkoutDigest=flip(capture['checkoutDigest'])),
                    dict(capture, worktreeId='b' * 32), dict(capture, repoId='c' * 32),
                    dict(capture, evidence={}), dict(capture, gitObjectFormat='sha256'),
                    {k: v for k, v in capture.items() if k != 'viewId'}, None, 'capture']
        for bad in variants:
            with self.subTest(capture=bad):
                self.no_match(bad, repository, worktree)
        self.assertIs(view.capture_matches(capture, repository, worktree), True)


class RefStates(CheckoutCase):
    def test_branch_switch_detach_and_return(self):
        repository, worktree = self.bind(self.root)
        main = self.cap(self.root, repository, worktree)
        self.git(self.root, 'checkout', '-q', '-b', 'feature/x')
        self.no_match(main, repository, worktree)
        feature = self.cap(self.root, repository, worktree)
        self.assertEqual((feature['branchKey'], feature['headOid']),
                         ('refs/heads/feature/x', main['headOid']))
        self.assertNotEqual(feature['viewId'], main['viewId'])
        self.git(self.root, 'checkout', '-q', '--detach')
        self.no_match(feature, repository, worktree)
        detached = self.cap(self.root, repository, worktree)
        self.assertEqual((detached['branchKey'], detached['headOid']),
                         ('detached:' + main['headOid'], main['headOid']))
        self.assertNotIn(detached['viewId'], (main['viewId'], feature['viewId']))
        self.git(self.root, 'checkout', '-q', 'main')
        back = self.cap(self.root, repository, worktree)
        self.assertEqual((back['branchKey'], back['viewId']), ('refs/heads/main', main['viewId']))

    def test_same_ref_other_lineage_keeps_view_but_not_head_or_digest(self):
        repository, worktree = self.bind(self.root)
        before = self.cap(self.root, repository, worktree)
        self.git(self.root, 'commit', '-q', '--amend', '-m', 'other lineage')
        head = self.out(self.root, 'rev-parse', 'HEAD')
        self.git(self.root, 'merge-base', '--is-ancestor', before['headOid'], head, ok=(1,))
        after = self.cap(self.root, repository, worktree)
        self.assertEqual((after['viewId'], after['branchKey'], after['headOid']),
                         (before['viewId'], 'refs/heads/main', head))
        self.assertNotEqual(after['checkoutDigest'], before['checkoutDigest'])
        self.no_match(before, repository, worktree)

    def test_unborn_ref_has_null_head_until_first_commit(self):
        root = self.repo('unborn', commit=False)
        repository, worktree = self.bind(root)
        unborn = self.cap(root, repository, worktree)
        self.assertEqual((unborn['branchKey'], unborn['headOid'], unborn['gitObjectFormat']),
                         ('refs/heads/main', None, 'sha1'))
        self.assertIs(view.capture_matches(unborn, repository, worktree), True)
        head = self.commit(root, 'a.txt', 'first\n')
        self.no_match(unborn, repository, worktree)
        born = self.cap(root, repository, worktree)
        self.assertEqual((born['viewId'], born['headOid']), (unborn['viewId'], head))

    def test_sha256_object_format_drives_oid_length(self):
        root = self.base / 'sha256'
        root.mkdir()
        if self.git(root, 'init', '-q', '--object-format=sha256', ok=(0, 128, 129)).returncode:
            self.skipTest('git without sha256 object format support')
        head = self.commit(root, 'a.txt', 'one\n')
        repository, worktree = self.bind(root)
        self.assertIn('sha256', list(leaves(repository)))
        capture = self.cap(root, repository, worktree)
        self.assertEqual((capture['gitObjectFormat'], capture['headOid'], len(head)),
                         ('sha256', head, 64))

    def test_non_git_project_has_single_non_git_view(self):
        plain = self.base / 'plain'
        plain.mkdir()
        (plain / 'note.md').write_text('note\n')
        repository, worktree = self.bind(plain)
        self.assertIn(canon(plain), list(leaves(repository)))
        self.assertIn(os.stat(plain).st_ino, list(leaves(repository)))
        capture = self.cap(plain, repository, worktree)
        self.assertEqual((capture['branchKey'], capture['headOid'], capture['gitObjectFormat'],
                          capture['projectRoot']), ('non-git', None, None, canon(plain)))
        self.assertRegex(capture['checkoutDigest'], HEX64)
        self.assertIs(view.capture_matches(capture, repository, worktree), True)
        self.refused(view.register_worktree, str(self.root), repository)
        self.git(plain, 'init', '-q')
        self.no_match(capture, repository, worktree)
        self.refused(self.cap, plain, repository, worktree)

    def test_bare_broken_or_symlinked_git_metadata_is_unavailable(self):
        names = ('bare', 'dangling', 'garbage', 'empty', 'symlinked')
        bare, dangling, garbage, empty, symlinked = (self.base / name for name in names)
        for path in (bare, dangling, garbage, empty, symlinked):
            path.mkdir()
        self.git(bare, 'init', '-q', '--bare', str(bare / '.git'))
        (dangling / '.git').write_text('gitdir: ' + str(self.base / 'missing') + '\n')
        (garbage / '.git').write_text('not a git file\n')
        (empty / '.git').mkdir()
        os.symlink(self.repo('outside') / '.git', symlinked / '.git')  # admin escape
        for root in (bare, dangling, garbage, empty, symlinked):
            with self.subTest(root=root.name):
                self.refused(view.register_repository, str(root))

    def test_corrupt_ref_never_reads_as_unborn(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        ref = self.root / '.git/refs/heads/main'
        self.assertTrue(ref.is_file())
        ref.write_text('garbage\n')
        self.refused(self.cap, self.root, repository, worktree)
        self.no_match(capture, repository, worktree)


class Topology(CheckoutCase):
    def test_linked_worktrees_share_repository_not_view(self):
        linked = self.base / 'linked'
        self.git(self.root, 'worktree', 'add', '-q', '--detach', str(linked), 'HEAD')
        self.git(self.root, 'checkout', '-q', '--detach')
        repository, main_wt = self.bind(self.root)
        linked_wt = view.register_worktree(str(linked), repository)
        self.assertIn(canon(self.root / '.git/worktrees/linked'), list(leaves(linked_wt)))
        main = self.cap(self.root, repository, main_wt)
        side = self.cap(linked, repository, linked_wt)
        self.assertEqual((side['repoId'], side['branchKey'], side['headOid']),
                         (main['repoId'], main['branchKey'], main['headOid']))
        self.assertNotEqual(side['worktreeId'], main['worktreeId'])
        self.assertNotEqual(side['viewId'], main['viewId'])
        self.assertEqual(side['projectRoot'], canon(linked))
        self.refused(self.cap, linked, repository, main_wt)
        self.refused(self.cap, self.root, repository, linked_wt)
        self.no_match(side, repository, main_wt)
        from_linked = view.register_repository(str(linked))
        self.assertIn(canon(self.root / '.git'), list(leaves(from_linked)))
        self.assertNotEqual(from_linked['repoId'], repository['repoId'])

    def test_clone_nested_and_submodule_do_not_share_repository(self):
        repository, _ = self.bind(self.root)
        clone = self.base / 'clone'
        self.git(self.base, 'clone', '-q', str(self.root), str(clone))
        self.refused(view.register_worktree, str(clone), repository)
        nested = self.root / 'pkg'
        nested.mkdir()
        self.refused(view.register_worktree, str(nested), repository)
        nested_repo = view.register_repository(str(nested))
        self.refused(view.register_worktree, str(self.root), nested_repo)
        nested_wt = view.register_worktree(str(nested), nested_repo)
        self.assertEqual(self.cap(nested, nested_repo, nested_wt)['projectRoot'], canon(nested))
        self.git(self.root, 'submodule', 'add', '-q', str(self.repo('lib')), 'sub')
        self.git(self.root, 'commit', '-q', '-m', 'add submodule')
        sub = self.root / 'sub'
        self.refused(view.register_worktree, str(sub), repository)
        sub_repo, sub_wt = self.bind(sub)
        self.assertIn(canon(self.root / '.git/modules/sub'), list(leaves(sub_repo)))
        self.assertNotEqual(self.cap(sub, sub_repo, sub_wt)['repoId'], repository['repoId'])

    def test_moved_replaced_or_readded_paths_are_not_adopted(self):
        linked = self.base / 'linked'
        self.git(self.root, 'worktree', 'add', '-q', '-b', 'side', str(linked))
        repository, worktree = self.bind(self.root)
        linked_wt = view.register_worktree(str(linked), repository)
        side = self.cap(linked, repository, linked_wt)
        # Retain the removed inodes so immediate allocator reuse cannot make
        # this binding test depend on the file system's allocation strategy.
        held = [os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                for path in (linked, self.root / '.git/worktrees/linked')]
        try:
            self.git(self.root, 'worktree', 'remove', str(linked))
            self.git(self.root, 'worktree', 'add', '-q', '-b', 'side2', str(linked))
        finally:
            for fd in held:
                os.close(fd)
        self.assertTrue((self.root / '.git/worktrees/linked').is_dir())  # admin path reused
        self.refused(self.cap, linked, repository, linked_wt)
        self.no_match(side, repository, linked_wt)
        capture = self.cap(self.root, repository, worktree)
        moved = self.base / 'moved'
        os.rename(self.root, moved)
        self.refused(self.cap, moved, repository, worktree)
        self.refused(view.register_worktree, str(moved), repository)
        self.no_match(capture, repository, worktree)
        self.repo('project')  # same path, new directory and repository
        self.refused(self.cap, self.root, repository, worktree)
        self.refused(view.register_worktree, str(self.root), repository)
        self.no_match(capture, repository, worktree)

    def test_case_and_unicode_aliases_share_one_binding(self):
        name = 'CaféAlias'
        root = self.repo(name)
        aliases = [self.base / spelled
                   for spelled in (name.swapcase(), unicodedata.normalize('NFC', name))
                   if spelled != name and (self.base / spelled).exists()
                   and os.path.samefile(self.base / spelled, root)]
        if not aliases:
            self.skipTest('case- and normalization-sensitive file system')
        expected = canon(root)
        repository, worktree = self.bind(root)
        first = self.cap(root, repository, worktree)
        self.assertEqual(first['projectRoot'], expected)
        for alias in aliases:
            with self.subTest(alias=alias.name):
                same = self.cap(alias, repository, worktree)
                self.assertEqual((same['projectRoot'], same['viewId'], same['checkoutDigest']),
                                 (expected, first['viewId'], first['checkoutDigest']))
                self.assertEqual(self.cap(root, *self.bind(alias))['projectRoot'], expected)
        self.assertIs(view.capture_matches(first, repository, worktree), True)


class IndexAndOperations(CheckoutCase):
    def test_index_change_invalidates_but_unstaged_bytes_are_not_read(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        index, config = self.root / '.git/index', self.root / '.git/config'
        before = (index.read_bytes(), index.stat().st_mtime_ns, config.read_bytes())
        (self.root / 'a.txt').write_text('unstaged edit\n')
        self.assertIs(view.capture_matches(capture, repository, worktree), True)
        self.assertEqual(self.cap(self.root, repository, worktree)['checkoutDigest'],
                         capture['checkoutDigest'])
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns, config.read_bytes()),
                         before, 'capture must not refresh the index or touch config')
        self.git(self.root, 'add', '--', 'a.txt')
        self.no_match(capture, repository, worktree)
        staged = self.cap(self.root, repository, worktree)
        self.assertEqual((staged['viewId'], staged['headOid']),
                         (capture['viewId'], capture['headOid']))
        self.assertNotEqual(staged['checkoutDigest'], capture['checkoutDigest'])

    def test_operation_markers_invalidate_capture(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        for name in MARKER_FILES + MARKER_DIRS:
            with self.subTest(marker=name):
                marker = self.root / '.git' / name
                marker.mkdir() if name in MARKER_DIRS else marker.write_text(capture['headOid'])
                self.no_match(capture, repository, worktree)
                try:
                    during = self.cap(self.root, repository, worktree)
                except view.CheckoutUnavailable as exc:  # refusing is also allowed
                    self.assertTrue(exc.reason)
                else:
                    self.assertNotEqual(during['checkoutDigest'], capture['checkoutDigest'])
                marker.rmdir() if name in MARKER_DIRS else marker.unlink()
                self.assertIs(view.capture_matches(capture, repository, worktree), True)

    def test_real_merge_and_linked_worktree_marker_invalidate(self):
        self.commit(self.root, 'c.txt', 'base\n')
        self.git(self.root, 'checkout', '-q', '-b', 'topic')
        self.commit(self.root, 'c.txt', 'topic\n')
        self.git(self.root, 'checkout', '-q', 'main')
        self.commit(self.root, 'c.txt', 'main\n')
        linked = self.base / 'linked'
        self.git(self.root, 'worktree', 'add', '-q', '-b', 'side', str(linked))
        repository, worktree = self.bind(self.root)
        linked_wt = view.register_worktree(str(linked), repository)
        capture = self.cap(self.root, repository, worktree)
        side = self.cap(linked, repository, linked_wt)
        self.git(self.root, 'merge', '-q', 'topic', ok=(1,))
        self.assertTrue((self.root / '.git/MERGE_HEAD').exists())
        self.no_match(capture, repository, worktree)
        self.assertIs(view.capture_matches(side, repository, linked_wt), True)
        self.git(self.root, 'merge', '--abort')
        (self.root / '.git/worktrees/linked/MERGE_HEAD').write_text(side['headOid'] + '\n')
        self.no_match(side, repository, linked_wt)

    def test_split_index_is_refused_or_fully_tracked(self):
        self.git(self.root, 'update-index', '--split-index')
        self.assertTrue(list((self.root / '.git').glob('sharedindex.*')))
        try:
            repository, worktree = self.bind(self.root)
            capture = self.cap(self.root, repository, worktree)
        except view.CheckoutUnavailable as exc:
            self.assertEqual(exc.reason, 'split_index_unsupported')
            return
        (self.root / 'b.txt').write_text('staged\n')
        self.git(self.root, 'add', '--', 'b.txt')
        self.no_match(capture, repository, worktree)

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_unreadable_or_symlinked_index_fails_closed(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        index = self.root / '.git/index'
        index.chmod(0)
        try:
            self.refused(self.cap, self.root, repository, worktree)
            self.no_match(capture, repository, worktree)
        finally:
            index.chmod(0o644)
        elsewhere = self.base / 'index-copy'
        shutil.copyfile(index, elsewhere)
        index.unlink()
        os.symlink(elsewhere, index)
        self.refused(self.cap, self.root, repository, worktree)
        self.no_match(capture, repository, worktree)


class GitEnvironmentAndBudget(CheckoutCase):
    def test_inherited_git_overrides_and_config_are_ignored(self):
        other = self.repo('other')
        self.commit(other, 'b.txt', 'other\n')
        repository, worktree = self.bind(self.root)
        clean = self.cap(self.root, repository, worktree)
        fake_home = self.base / 'home'
        fake_home.mkdir()
        hostile = fake_home / '.gitconfig'
        hostile.write_text('[core]\n\tbare = true\n')
        dirty = {'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(other),
                 'GIT_INDEX_FILE': str(other / '.git/index'), 'GIT_COMMON_DIR': str(other / '.git'),
                 'GIT_OBJECT_DIRECTORY': str(other / '.git/objects'), 'GIT_NAMESPACE': 'ns',
                 'GIT_CONFIG_PARAMETERS': "'core.bare'='true'", 'GIT_CONFIG_COUNT': '1',
                 'GIT_CONFIG_KEY_0': 'core.bare', 'GIT_CONFIG_VALUE_0': 'true',
                 'GIT_CONFIG_GLOBAL': str(hostile), 'GIT_CONFIG_SYSTEM': str(hostile),
                 'GIT_CONFIG': str(hostile), 'HOME': str(fake_home),
                 'XDG_CONFIG_HOME': str(fake_home)}
        calls = []

        class Spy(subprocess.Popen):
            def __init__(self, args, *rest, **kwargs):
                calls.append(([args] if isinstance(args, str) else list(args), kwargs.get('env')))
                super().__init__(args, *rest, **kwargs)

        with mock.patch.dict(os.environ, dirty), mock.patch.object(subprocess, 'Popen', Spy):
            observed = self.cap(self.root, *self.bind(self.root))
            again = self.cap(self.root, repository, worktree)
            self.assertIs(view.capture_matches(clean, repository, worktree), True)
        self.assertEqual(observed['headOid'], clean['headOid'])
        for key in ('headOid', 'branchKey', 'viewId', 'checkoutDigest', 'projectRoot'):
            self.assertEqual(again[key], clean[key], key)
        self.assertTrue(calls, 'no git subprocess observed through subprocess.Popen')
        for argv, env in calls:
            with self.subTest(argv=argv):
                self.assertEqual(os.path.basename(str(argv[0])), 'git')
                self.assertIsNotNone(env, 'git must get an explicit sanitized env')
                for key, value in dirty.items():
                    if key.startswith('GIT_'):
                        self.assertNotEqual(env.get(key), value, key)
                self.assertEqual(env.get('GIT_CONFIG_GLOBAL'), os.devnull)
                self.assertTrue(env.get('GIT_CONFIG_NOSYSTEM') == '1'
                                or env.get('GIT_CONFIG_SYSTEM') == os.devnull)
                self.assertTrue(env.get('GIT_OPTIONAL_LOCKS') == '0'
                                or '--no-optional-locks' in argv)
                self.assertEqual(env.get('GIT_TERMINAL_PROMPT'), '0')

    def test_expired_or_slow_git_deadline_refuses_and_leaves_no_process(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        expired = time.monotonic() - 1
        self.refused(view.register_repository, str(self.root), deadline=expired)
        self.refused(view.register_worktree, str(self.root), repository, deadline=expired)
        self.refused(self.cap, self.root, repository, worktree, deadline=expired)
        self.no_match(capture, repository, worktree, deadline=expired)
        for number, invalid in enumerate((float('nan'), float('inf'), True, 'later', 10 ** 400)):
            with self.subTest(invalid_deadline=number):
                self.refused(view.register_repository, str(self.root), deadline=invalid)
                self.refused(view.register_worktree, str(self.root), repository, deadline=invalid)
                self.refused(self.cap, self.root, repository, worktree, deadline=invalid)
                self.no_match(capture, repository, worktree, deadline=invalid)
        pidfile = self.base / 'fake.pid'
        with self.fake_git(f'echo $$ >> {shlex.quote(str(pidfile))}\n'
                           f'exec {shutil.which("sleep")} 30\n'):
            for call in (lambda d: self.cap(self.root, repository, worktree, deadline=d),
                         lambda d: view.register_repository(str(self.root), deadline=d)):
                started = time.monotonic()
                self.refused(call, started + 1.0)
                self.assertLess(time.monotonic() - started, 10)
            started = time.monotonic()
            self.no_match(capture, repository, worktree, deadline=started + 1.0)
            self.assertLess(time.monotonic() - started, 10)
        self.assert_no_process(pidfile)

    def test_oversized_output_and_git_failure_raise_not_non_git(self):
        repository, worktree = self.bind(self.root)
        pidfile = self.base / 'fake.pid'
        with self.fake_git(f'echo $$ >> {shlex.quote(str(pidfile))}\n'
                           f'exec {shutil.which("head")} -c 67108864 /dev/zero\n'):
            started = time.monotonic()
            self.refused(self.cap, self.root, repository, worktree, deadline=started + 20)
            self.refused(view.register_repository, str(self.root), deadline=started + 20)
            self.assertLess(time.monotonic() - started, 40)
        self.assert_no_process(pidfile)
        with self.fake_git('echo "fatal: simulated failure" >&2\nexit 128\n'):
            self.refused(view.register_repository, str(self.root))
            self.refused(self.cap, self.root, repository, worktree)
            self.refused(view.register_repository, str(self.repo('unborn', commit=False)))

    def test_moving_ref_is_rejected_as_mixed_snapshot(self):
        repository, worktree = self.bind(self.root)
        capture = self.cap(self.root, repository, worktree)
        # After every git call the branch moves to a fresh child commit, so no
        # two observations ever agree; a capture must refuse, not mix them.
        real, root = shlex.quote(REAL_GIT), shlex.quote(str(self.root))
        body = (f'{real} "$@"\nstatus=$?\n'
                f'parent=$({real} -C {root} rev-parse -q --verify refs/heads/main)\n'
                f'next=$({real} -C {root} -c user.name=f -c user.email=f@example.invalid '
                f'commit-tree "$parent^{{tree}}" -p "$parent" -m flap)\n'
                f'{real} -C {root} update-ref refs/heads/main "$next"\nexit $status\n')
        with self.fake_git(body):
            started = time.monotonic()
            self.refused(self.cap, self.root, repository, worktree, deadline=started + 10)
            self.assertLess(time.monotonic() - started, 20)
            self.no_match(capture, repository, worktree, deadline=time.monotonic() + 10)


class NoActivation(CheckoutCase):
    def tree(self, *tops):
        return {str(path): (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino)
                for top in tops for path in (top, *top.rglob('*'))
                for info in (os.lstat(path),)}

    def test_api_writes_nothing_and_no_live_route_uses_it(self):
        linked = self.base / 'linked'
        self.git(self.root, 'worktree', 'add', '-q', '-b', 'side', str(linked))
        tops = [self.base]
        home = Path(os.environ['HOME']).resolve()
        if home.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            tops.append(home)  # the wrapper's temp HOME; never a real one
        before = self.tree(*tops)
        repository, worktree = self.bind(self.root)
        linked_wt = view.register_worktree(str(linked), repository)
        for root, binding in ((self.root, worktree), (linked, linked_wt)):
            capture = self.cap(root, repository, binding)
            self.assertIs(view.capture_matches(capture, repository, binding), True)
        self.assertEqual(self.tree(*tops), before, 'the API must create no state, lock or DB')
        loaded = set(sys.modules) - MODULES_BEFORE
        self.assertEqual(sorted(m for m in loaded if m.startswith(ROUTE_PREFIXES)), [])
        users = [str(path.relative_to(REPO))
                 for path in [*(REPO / 'core').rglob('*.py'), REPO / 'hooks/run-hook']
                 if path.name != 'laya_checkout_view.py'
                 and 'laya_checkout_view' in path.read_text(encoding='utf-8', errors='replace')]
        self.assertEqual(users, [], 'PR 1 must not activate checkout capture in a live path')


if __name__ == '__main__':
    unittest.main()
