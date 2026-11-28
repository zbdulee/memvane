"""SSE hub limits and frames, the stat watcher, target ids, branch names and card counts."""
from __future__ import annotations

import json
import os
import subprocess

from memvane.ui import read_adapters, sse
from memvane.ui.types import MAX_SSE_CLIENTS, parse_target_id, target_id, worktree_hash
from tests.support.case import FixtureCase


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


class HubTests(FixtureCase):
    def test_the_hub_holds_at_most_sixteen_streams(self):
        hub = sse.SseHub()
        clients = [hub.register() for _ in range(MAX_SSE_CLIENTS)]
        self.assertTrue(all(clients))
        self.assertIsNone(hub.register())
        self.assertEqual(hub.active_clients, MAX_SSE_CLIENTS)
        hub.unregister(clients[0])
        self.assertIsNotNone(hub.register())

    def test_close_time_drives_the_reconnect_grace(self):
        clock = Clock()
        hub = sse.SseHub(clock=clock, wall=lambda: 1234.0)
        client = hub.register()
        self.assertFalse(hub.grace_active(10))
        hub.unregister(client)
        self.assertEqual((hub.active_clients, hub.last_closed_at), (0, 1234.0))
        clock.now += 9
        self.assertTrue(hub.grace_active(10))
        clock.now += 1
        self.assertFalse(hub.grace_active(10))

    def test_broadcast_reaches_every_stream_and_drops_a_full_one(self):
        hub = sse.SseHub()
        slow, fast = hub.register(), hub.register()
        for _ in range(sse.QUEUE_LIMIT):
            slow.push(b'x')
        self.assertEqual(hub.broadcast('runtime_updated', {'ts': 't'}), 1)
        self.assertEqual(hub.active_clients, 1)
        self.assertTrue(slow.closed.is_set())
        self.assertEqual(fast.next(0), b'event: runtime_updated\ndata: {"ts":"t"}\n\n')

    def test_drain_tells_streams_and_refuses_new_ones(self):
        hub = sse.SseHub()
        client = hub.register()
        hub.drain('user_stop')
        frame = client.next(0)
        self.assertTrue(frame.startswith(b'event: server_draining\n'))
        self.assertEqual(json.loads(frame.split(b'data: ', 1)[1]), {'reason': 'user_stop'})
        self.assertIsNone(hub.register())

    def test_only_known_events_are_framed(self):
        with self.assertRaisesRegex(ValueError, 'ui_sse_event_unknown'):
            sse.frame('custom', {})


class WatcherTests(FixtureCase):
    def test_size_change_and_rotation_are_changes(self):
        path = self.tmp / 'events.jsonl'
        path.write_text('a\n')
        watcher = sse.FileWatcher()
        watched = {str(path): 'install:a'}
        self.assertEqual(watcher.poll(watched), [])
        self.assertEqual(watcher.poll(watched), [])
        path.write_text('a\nb\n')
        self.assertEqual(watcher.poll(watched), ['install:a'])
        replacement = self.tmp / 'new.jsonl'
        replacement.write_text('a\nb\n')
        os.replace(replacement, path)
        self.assertEqual(watcher.poll(watched), ['install:a'])
        path.unlink()
        self.assertEqual(watcher.poll(watched), ['install:a'])


class TargetIdTests(FixtureCase):
    def test_main_and_worktree_ids(self):
        install = 'a' * 32
        self.assertEqual(target_id(install, '/p', 'main'), install)
        wt = target_id(install, '/p-wt', 'worktree')
        self.assertEqual(wt, f'{install}:{worktree_hash("/p-wt")}')
        self.assertEqual(parse_target_id(wt), (install, worktree_hash('/p-wt')))
        self.assertEqual(parse_target_id(install), (install, None))
        for bad in ('A' * 32, 'a' * 31, f'{install}:xyz', f'{install}:', None, 7):
            self.assertIsNone(parse_target_id(bad), bad)


class BranchTests(FixtureCase):
    def git(self, root, *args):
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}
        return subprocess.run(['git', '-C', str(root), '-c', 'init.defaultBranch=main', '-c', 'user.name=F',
                               '-c', 'user.email=f@example.invalid', '-c', 'commit.gpgsign=false', *args],
                              check=True, capture_output=True, text=True, env=env).stdout.strip()

    def test_branch_detached_worktree_and_non_git(self):
        repo = self.tmp / 'repo'
        repo.mkdir()
        self.git(repo, 'init', '-q')
        self.git(repo, 'commit', '--allow-empty', '-qm', 'one')
        self.assertEqual(read_adapters.read_branch(repo), 'main')
        sha = self.git(repo, 'rev-parse', 'HEAD')
        linked = self.tmp / 'linked'
        self.git(repo, 'worktree', 'add', '-q', '-b', 'feature/x', str(linked))
        self.assertEqual(read_adapters.read_branch(linked), 'feature/x')
        self.git(repo, 'checkout', '-q', '--detach')
        self.assertEqual(read_adapters.read_branch(repo), 'detached@' + sha[:7])
        plain = self.tmp / 'plain'
        plain.mkdir()
        self.assertEqual(read_adapters.read_branch(plain), 'non-git')

    def test_unreadable_or_symlinked_head_is_unknown(self):
        repo = self.tmp / 'fake'
        (repo / '.git').mkdir(parents=True)
        (repo / '.git' / 'HEAD').write_text('garbage\n')
        self.assertEqual(read_adapters.read_branch(repo), 'unknown')
        (repo / '.git' / 'HEAD').unlink()
        target = self.tmp / 'head-target'
        target.write_text('ref: refs/heads/main\n')
        os.symlink(target, repo / '.git' / 'HEAD')
        self.assertEqual(read_adapters.read_branch(repo), 'unknown')
        (repo / '.git' / 'HEAD').unlink()
        (repo / '.git' / 'HEAD').write_bytes(b'ref: refs/heads/' + b'x' * 5000)
        self.assertEqual(read_adapters.read_branch(repo), 'unknown')
        other = self.tmp / 'odd'
        other.mkdir()
        (other / '.git').write_text('not a pointer\n')
        self.assertEqual(read_adapters.read_branch(other), 'unknown')


class CardCountTests(FixtureCase):
    def test_counts_by_frontmatter_status_without_following_symlinks(self):
        wiki = self.tmp / 'proj' / '.memvane' / 'wiki'
        (wiki / 'concepts').mkdir(parents=True)
        (wiki / 'entities').mkdir()
        (wiki / 'concepts' / 'a.md').write_text('---\ntitle: "A"\nstatus: verified\n---\n# A\nstatus: generated\n')
        (wiki / 'entities' / 'b.md').write_text('---\ntitle: "B"\nstatus: generated\n---\n# B\n')
        (wiki / 'entities' / 'c.md').write_text('# no front matter\n')
        (wiki / 'notes.txt').write_text('status: verified\n')
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'd.md').write_text('---\nstatus: verified\n---\n')
        os.symlink(outside, wiki / 'linked')
        counts = read_adapters.card_counts(self.tmp / 'proj')
        self.assertEqual(counts, {'verified': 1, 'generated': 1, 'other': 1, 'truncated': False})
        self.assertTrue(read_adapters.card_counts(self.tmp / 'proj', limit=2)['truncated'])
        self.assertEqual(read_adapters.card_counts(self.tmp / 'missing'),
                         {'verified': 0, 'generated': 0, 'other': 0, 'truncated': False})

    def test_card_counts_ignores_symlinked_memvane_or_wiki_root(self):
        outside = self.tmp / 'outside_wiki'
        (outside / 'concepts').mkdir(parents=True)
        (outside / 'concepts' / 'x.md').write_text('---\nstatus: verified\n---\n')
        sym_wiki = self.tmp / 'sym_wiki_proj'
        (sym_wiki / '.memvane').mkdir(parents=True)
        os.symlink(outside, sym_wiki / '.memvane' / 'wiki')
        self.assertEqual(read_adapters.card_counts(sym_wiki),
                         {'verified': 0, 'generated': 0, 'other': 0, 'truncated': False})
        sym_mv = self.tmp / 'sym_mv_proj'
        sym_mv.mkdir()
        os.symlink(outside, sym_mv / '.memvane')
        self.assertEqual(read_adapters.card_counts(sym_mv),
                         {'verified': 0, 'generated': 0, 'other': 0, 'truncated': False})
