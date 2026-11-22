"""Synthetic Git checkout cache; no live project or runtime."""
import os
import json
import subprocess
import sys
from pathlib import Path
import time
import unittest
from unittest import mock

from memvane.store.checkout import checkout_view as view
from memvane.store.tests import support as existing

REPO = Path(__file__).resolve().parents[4]


class GitChildren(existing.CheckoutCase):
    """How many Git children one observation costs, on every platform (the count, not a duration)."""

    def observed(self, call):
        """``[git arguments]`` and ``[program]`` of every Git child ``call()`` started."""
        commands, programs = [], []
        real = view.subprocess.Popen

        def started(argv, *args, **kwargs):
            if '-c' in argv and 'log.showSignature=false' in argv:  # Only this module's own children.
                programs.append(argv[0])
                commands.append(argv[7:])
            return real(argv, *args, **kwargs)
        with mock.patch.object(view.subprocess, 'Popen', side_effect=started):
            value = call()
        return value, commands, programs

    def test_one_full_observation_starts_twelve_git_children_and_checks_the_version_once(self):
        repository, worktree = self.bind(self.root)
        capture, commands, _programs = self.observed(
            lambda: view.capture_view(str(self.root), repository, worktree, fresh=True))
        counted = [command for command in commands if command != ['--exec-path']]
        self.assertEqual(len(counted), 12, counted)
        self.assertEqual(counted.count(['--version']), 1)
        self.assertEqual(counted[0], ['--version'])
        # The two observations still read the ref and HEAD twice each.
        self.assertEqual(sum(command[0] == 'symbolic-ref' for command in counted), 4)
        self.assertEqual(sum(command[:2] == ['rev-parse', '--verify'] for command in counted), 4)
        self.assertEqual(capture['branchKey'], 'refs/heads/main')

    def test_an_unsupported_git_version_is_still_refused(self):
        repository, worktree = self.bind(self.root)
        real = view._git

        def old(args, *rest, **kwargs):
            return (0, 'git version 2.30.0') if args == ['--version'] else real(args, *rest, **kwargs)
        with mock.patch.object(view, '_git', side_effect=old):
            with self.assertRaises(view.CheckoutUnavailable) as caught:
                view.capture_view(str(self.root), repository, worktree, fresh=True)
        self.assertEqual(caught.exception.reason, 'git_version_unsupported')

    def test_git_runs_as_the_binary_the_fingerprint_proved_and_as_git_until_then(self):
        repository, worktree = self.bind(self.root)
        view._capture_cache.tool = None
        _capture, commands, programs = self.observed(
            lambda: view.capture_view(str(self.root), repository, worktree, fresh=True))
        proved = getattr(view._capture_cache, 'tool', None)
        if proved is None:
            # No shim to see through (another Git on PATH, another platform, or no fingerprint here).
            self.assertEqual(set(programs), {'git'})
            return
        delegated = str(proved[1])
        self.assertTrue(delegated.endswith('/bin/git') and delegated.startswith('/'), delegated)
        # The proof itself goes through the shim; everything after it does not.
        self.assertEqual([program for program, command in zip(programs, commands) if command == ['--exec-path']],
                         ['git'])
        self.assertEqual({program for program, command in zip(programs, commands) if command != ['--exec-path']},
                         {delegated})
        # The proof belongs to that capture alone: a registration right after it asks ``git`` on PATH again.
        _value, commands, programs = self.observed(lambda: view.register_repository(str(self.root)))
        self.assertEqual((len(commands), set(programs)), (12, {'git'}))
        # A capture whose fingerprint cannot be taken starts again from ``git``.
        with mock.patch.object(view, '_cache_filesystem', return_value=None):
            _capture, commands, programs = self.observed(
                lambda: view.capture_view(str(self.root), repository, worktree, fresh=True))
        self.assertEqual((len(commands), set(programs)), (12, {'git'}))


@unittest.skipUnless(sys.platform == 'darwin', 'fast path requires a known APFS timestamp contract')
class CheckoutCacheChecks(existing.CheckoutCase):
    def setUp(self):
        super().setUp()
        if view._cache_filesystem(self.root) is None:
            self.skipTest('fast path requires APFS')
        now_ns = time.time_ns
        clock = mock.patch.object(view.time, 'time_ns', side_effect=lambda: now_ns() + 2_000_000_000)
        clock.start()
        self.addCleanup(clock.stop)

    def test_stable_checkout_avoids_repeating_git_after_full_proof(self):
        repository, worktree = self.bind(self.root)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as git:
            observed = self.cap(self.root, repository, worktree)
        self.assertEqual(observed, baseline)
        self.assertEqual(git.call_count, 0, 'unchanged fresh-stat capture should not spawn Git')

    def test_ref_index_and_operation_changes_force_a_fresh_capture(self):
        repository, worktree = self.bind(self.root)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as warm:
            self.cap(self.root, repository, worktree)
        self.assertEqual(warm.call_count, 0)
        self.commit(self.root, 'a.txt', 'changed\n')
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            changed = self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)
        self.assertNotEqual(changed['headOid'], baseline['headOid'])
        self.cap(self.root, repository, worktree)
        (self.root / 'a.txt').write_text('staged change\n', encoding='utf8')
        self.git(self.root, 'add', '--', 'a.txt')
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            staged = self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)
        self.assertNotEqual(staged['checkoutDigest'], changed['checkoutDigest'])
        self.cap(self.root, repository, worktree)
        (self.root / '.git' / 'MERGE_HEAD').write_text(changed['headOid'] + '\n', encoding='ascii')
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            merging = self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)
        self.assertTrue(merging['evidence']['operations'])

    def test_recent_same_tick_and_packed_head_cache_invalidation(self):
        repository, worktree = self.bind(self.root)
        self.cap(self.root, repository, worktree)
        head = self.root / '.git' / 'HEAD'
        head.touch()
        with mock.patch.object(view.time, 'time_ns', return_value=head.stat().st_mtime_ns):
            self.cap(self.root, repository, worktree)
            with mock.patch.object(view, '_git', wraps=view._git) as recent:
                self.cap(self.root, repository, worktree)
        self.assertGreater(recent.call_count, 0)
        self.git(self.root, 'pack-refs', '--all', '--prune')
        expected = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as packed:
            actual = self.cap(self.root, repository, worktree)
        self.assertEqual(actual, expected)
        self.assertEqual(packed.call_count, 0)
        self.commit(self.root, 'a.txt', 'new packed revision\n')
        newer = self.cap(self.root, repository, worktree)
        self.assertNotEqual(newer['headOid'], expected['headOid'])
        self.git(self.root, 'pack-refs', '--all', '--prune')
        with mock.patch.object(view, '_git', wraps=view._git) as changed:
            repacked = self.cap(self.root, repository, worktree)
        self.assertEqual(repacked, newer)
        self.assertGreater(changed.call_count, 0)

    def test_mutating_returned_capture_does_not_mutate_cache(self):
        repository, worktree = self.bind(self.root)
        expected = self.cap(self.root, repository, worktree)
        changed = self.cap(self.root, repository, worktree)
        changed['evidence']['branchKey'] = 'caller-tampered'
        with mock.patch.object(view, '_git', wraps=view._git) as warm:
            actual = self.cap(self.root, repository, worktree)
        self.assertEqual(actual, expected)
        self.assertEqual(warm.call_count, 0)

    def test_config_include_disables_fast_path(self):
        repository, worktree = self.bind(self.root)
        with (self.root / '.git' / 'config').open('a', encoding='utf8') as out:
            out.write('\n[include]\n    path = absent-fixture.conf\n')
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)

    def test_missing_head_object_cannot_reuse_cached_proof(self):
        repository, worktree = self.bind(self.root)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as warm:
            self.cap(self.root, repository, worktree)
        self.assertEqual(warm.call_count, 0)
        oid = baseline['headOid']
        obj = self.root / '.git' / 'objects' / oid[:2] / oid[2:]
        held = obj.with_name(obj.name + '.fixture-held')
        obj.rename(held)
        try:
            with mock.patch.object(view, '_git', wraps=view._git) as cold:
                with self.assertRaises(view.CheckoutUnavailable):
                    self.cap(self.root, repository, worktree)
            self.assertGreater(cold.call_count, 0)
        finally:
            held.rename(obj)

    def test_future_timestamp_and_clock_reversal_force_cold_path(self):
        repository, worktree = self.bind(self.root)
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view.time, 'time_ns', return_value=1),                 mock.patch.object(view, '_git', wraps=view._git) as backwards:
            self.cap(self.root, repository, worktree)
        self.assertGreater(backwards.call_count, 0)
        head = self.root / '.git' / 'HEAD'
        future = time.time() + 30
        os.utime(head, (future, future))
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as future_git:
            self.cap(self.root, repository, worktree)
        self.assertGreater(future_git.call_count, 0)

    def test_symlinked_reference_parent_cannot_use_fast_path(self):
        repository, worktree = self.bind(self.root)
        baseline = self.cap(self.root, repository, worktree)
        self.assertEqual(baseline['branchKey'], 'refs/heads/main')
        heads = self.root / '.git' / 'refs' / 'heads'
        held = heads.with_name('heads-fixture-held')
        heads.rename(held)
        heads.symlink_to(held, target_is_directory=True)
        original_file = view._file
        def guarded_file(path, *args, **kwargs):
            if Path(path) == heads / 'main':
                self.fail('a ref under a symlinked parent must not be read by the cache')
            return original_file(path, *args, **kwargs)
        try:
            with mock.patch.object(view, '_file', side_effect=guarded_file), \
                    mock.patch.object(view, '_git', wraps=view._git) as cold:
                try:
                    self.cap(self.root, repository, worktree)
                except view.CheckoutUnavailable:
                    pass
            self.assertGreater(cold.call_count, 0)
        finally:
            heads.unlink()
            held.rename(heads)

    def test_publication_can_require_a_fresh_git_proof(self):
        repository, worktree = self.bind(self.root)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as git:
            self.assertTrue(view.capture_matches(baseline, repository, worktree, fresh=True))
        self.assertGreater(git.call_count, 0)

    def test_fresh_proof_populates_cache_for_next_unchanged_capture(self):
        repository, worktree = self.bind(self.root)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as git:
            self.assertTrue(view.capture_matches(baseline, repository, worktree, fresh=True))
            self.assertGreater(git.call_count, 0)
            git.reset_mock()
            self.assertEqual(self.cap(self.root, repository, worktree), baseline)
            self.assertEqual(git.call_count, 0)

    def test_unknown_filesystem_uses_the_full_git_path(self):
        repository, worktree = self.bind(self.root)
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_cache_filesystem', return_value=None), \
                mock.patch.object(view, '_git', wraps=view._git) as cold:
            self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)

    def test_packed_replace_reference_and_alternates_disable_fast_path(self):
        repository, worktree = self.bind(self.root)
        oid = self.out(self.root, 'rev-parse', 'HEAD')
        self.git(self.root, 'pack-refs', '--all', '--prune')
        loose = self.root / '.git' / 'refs' / 'heads' / 'main'
        loose.parent.mkdir(parents=True, exist_ok=True)
        loose.write_text(oid + '\n', encoding='ascii')
        packed = self.root / '.git' / 'packed-refs'
        replacement = oid + ' refs/replace/' + '0' * 40 + '\n'
        with packed.open('a', encoding='ascii') as out:
            out.write(replacement)
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as packed_git:
            self.cap(self.root, repository, worktree)
        self.assertGreater(packed_git.call_count, 0)
        packed.write_text(packed.read_text(encoding='ascii').replace(replacement, ''), encoding='ascii')
        info = self.root / '.git' / 'objects' / 'info'
        info.mkdir(exist_ok=True)
        alternate = self.base / 'alternate-objects'
        alternate.mkdir()
        (info / 'alternates').write_text(str(alternate) + '\n', encoding='utf8')
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as alternate_git:
            self.cap(self.root, repository, worktree)
        self.assertGreater(alternate_git.call_count, 0)

    def test_records_from_another_boot_keep_the_cache(self):
        with self.other_boot():
            repository, worktree = self.bind(self.root)
        live = view._directory(self.root)
        self.assertEqual(worktree['project'], {**live, 'dev': live['dev'] + 7})
        self.assertEqual(repository['commonGitDir']['dev'], live['dev'] + 7)
        sealed = json.dumps([repository, worktree], sort_keys=True)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as git:
            observed = self.cap(self.root, repository, worktree)
        self.assertEqual(observed, baseline)
        self.assertEqual(git.call_count, 0)
        self.assertEqual(baseline['evidence']['project'], live)
        self.assertEqual(json.dumps([repository, worktree], sort_keys=True), sealed)

    def test_worktree_registered_after_the_device_moved_keeps_the_cache(self):
        with self.other_boot():
            repository = view.register_repository(str(self.root))
        worktree = view.register_worktree(str(self.root), repository)
        live = view._directory(self.root)
        self.assertEqual(worktree['project'], live)
        self.assertEqual(repository['commonGitDir']['dev'], live['dev'] + 7)
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as git:
            self.cap(self.root, repository, worktree)
        self.assertEqual(git.call_count, 0)

    def test_another_boot_record_with_a_moved_inode_is_refused(self):
        with self.other_boot():
            repository, worktree = self.bind(self.root)
        moved = {**worktree, 'checkout': {**worktree['checkout'], 'ino': worktree['checkout']['ino'] + 1}}
        self.assertIsNone(view._cache_fingerprint(str(self.root), repository, moved, time.monotonic() + 5))
        with self.assertRaises(view.CheckoutUnavailable) as raised:
            self.cap(self.root, repository, moved)
        self.assertEqual(raised.exception.reason, 'worktree_binding_changed')

    def test_new_process_does_not_inherit_cached_proof(self):
        repository, worktree = self.bind(self.root)
        self.cap(self.root, repository, worktree)
        payload = json.dumps([str(self.root), repository, worktree])
        program = """import json,sys; from unittest import mock
import memvane.store.checkout.checkout_view as v
root,repo,worktree=json.loads(sys.argv[1])
with mock.patch.object(v, '_git', wraps=v._git) as git:
    v.capture_view(root, repo, worktree)
    print(git.call_count)
"""
        child = subprocess.run([sys.executable, '-B', '-c', program, payload], cwd=str(REPO),
                               text=True, capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertGreater(int(child.stdout.strip()), 0)


if __name__ == '__main__':
    unittest.main()
