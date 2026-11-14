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


@unittest.skipUnless(sys.platform == 'darwin', 'fast path requires a known APFS timestamp contract')
class CheckoutCacheChecks(existing.CheckoutCase):
    def setUp(self):
        super().setUp()
        if view._cache_filesystem(self.root) is None:
            self.skipTest('fast path requires APFS')

    def test_stable_checkout_avoids_repeating_git_after_full_proof(self):
        repository, worktree = self.bind(self.root)
        # A recent write must first leave the filesystem timestamp tick.
        time.sleep(1.1)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as git:
            observed = self.cap(self.root, repository, worktree)
        self.assertEqual(observed, baseline)
        self.assertEqual(git.call_count, 0, 'unchanged fresh-stat capture should not spawn Git')

    def test_ref_index_and_operation_changes_force_a_fresh_capture(self):
        repository, worktree = self.bind(self.root)
        time.sleep(1.1)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as warm:
            self.cap(self.root, repository, worktree)
        self.assertEqual(warm.call_count, 0)
        self.commit(self.root, 'a.txt', 'changed\n')
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            changed = self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)
        self.assertNotEqual(changed['headOid'], baseline['headOid'])
        time.sleep(1.1)
        self.cap(self.root, repository, worktree)
        (self.root / 'a.txt').write_text('staged change\n', encoding='utf8')
        self.git(self.root, 'add', '--', 'a.txt')
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            staged = self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)
        self.assertNotEqual(staged['checkoutDigest'], changed['checkoutDigest'])
        time.sleep(1.1)
        self.cap(self.root, repository, worktree)
        (self.root / '.git' / 'MERGE_HEAD').write_text(changed['headOid'] + '\n', encoding='ascii')
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            merging = self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)
        self.assertTrue(merging['evidence']['operations'])

    def test_recent_same_tick_and_packed_head_cache_invalidation(self):
        repository, worktree = self.bind(self.root)
        time.sleep(1.1)
        self.cap(self.root, repository, worktree)
        head = self.root / '.git' / 'HEAD'
        head.touch()
        with mock.patch.object(view.time, 'time_ns', return_value=head.stat().st_mtime_ns):
            self.cap(self.root, repository, worktree)
            with mock.patch.object(view, '_git', wraps=view._git) as recent:
                self.cap(self.root, repository, worktree)
        self.assertGreater(recent.call_count, 0)
        self.git(self.root, 'pack-refs', '--all', '--prune')
        time.sleep(1.1)
        expected = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as packed:
            actual = self.cap(self.root, repository, worktree)
        self.assertEqual(actual, expected)
        self.assertEqual(packed.call_count, 0)
        self.commit(self.root, 'a.txt', 'new packed revision\n')
        time.sleep(1.1)
        newer = self.cap(self.root, repository, worktree)
        self.assertNotEqual(newer['headOid'], expected['headOid'])
        self.git(self.root, 'pack-refs', '--all', '--prune')
        with mock.patch.object(view, '_git', wraps=view._git) as changed:
            repacked = self.cap(self.root, repository, worktree)
        self.assertEqual(repacked, newer)
        self.assertGreater(changed.call_count, 0)

    def test_mutating_returned_capture_does_not_mutate_cache(self):
        repository, worktree = self.bind(self.root)
        time.sleep(1.1)
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
        time.sleep(1.1)
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as cold:
            self.cap(self.root, repository, worktree)
        self.assertGreater(cold.call_count, 0)

    def test_missing_head_object_cannot_reuse_cached_proof(self):
        repository, worktree = self.bind(self.root)
        time.sleep(1.1)
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
        time.sleep(1.1)
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
        time.sleep(1.1)
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
        time.sleep(1.1)
        baseline = self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as git:
            self.assertTrue(view.capture_matches(baseline, repository, worktree, fresh=True))
        self.assertGreater(git.call_count, 0)

    def test_unknown_filesystem_uses_the_full_git_path(self):
        repository, worktree = self.bind(self.root)
        time.sleep(1.1)
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
        time.sleep(1.1)
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
        time.sleep(1.1)
        self.cap(self.root, repository, worktree)
        with mock.patch.object(view, '_git', wraps=view._git) as alternate_git:
            self.cap(self.root, repository, worktree)
        self.assertGreater(alternate_git.call_count, 0)

    def test_new_process_does_not_inherit_cached_proof(self):
        repository, worktree = self.bind(self.root)
        time.sleep(1.1)
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
