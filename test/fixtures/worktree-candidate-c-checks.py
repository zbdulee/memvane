#!/usr/bin/env python3
"""Synthetic Candidate C membership/query experiments; no real QMD or files."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import worktree_wiki_membership as membership
import worktree_query_slice as query


def h(body):
    return hashlib.sha256(body.encode()).hexdigest()


class WorktreeMembership(unittest.TestCase):
    def test_actual_files_not_branch_name_determine_membership(self):
        for hint in ('main', 'master', 'trunk', None, 'feature/story'):
            planned = membership.plan(None, {'chapter.md': h('dirty wiki')},
                                      worktree='wt', generation='g1',
                                      raw_generation='raw-3', branch_hint=hint)
            self.assertEqual(dict(planned.view.members),
                             {'chapter.md': h('dirty wiki')})
            self.assertEqual(planned.view.raw_generation, 'raw-3')
            self.assertEqual(planned.view.generation, 'g1')
        # Detached HEAD and uncommitted content have no branch dependency.
        detached = membership.plan(None, {'chapter.md': h('uncommitted')},
                                   worktree='detached', generation='g1',
                                   raw_generation='raw-4')
        self.assertEqual(dict(detached.view.members)['chapter.md'], h('uncommitted'))

    def test_add_edit_delete_rename_and_unknown_preservation(self):
        first = membership.plan(None, {'a.md': h('A'), 'b.md': h('B')},
                                worktree='wt', generation='g1', raw_generation='raw-1')
        self.assertEqual(first.added, ('a.md', 'b.md'))
        second = membership.plan(first.view,
                                 {'a.md': h('A edited'), 'c.md': h('B')},
                                 worktree='wt', generation='g2', raw_generation='raw-1')
        self.assertEqual((second.added, second.edited, second.deleted),
                         (('c.md',), ('a.md',), ('b.md',)))
        self.assertEqual(dict(second.view.members),
                         {'a.md': h('A edited'), 'c.md': h('B')})
        # An unreadable directory is not proof that its old wiki pages died.
        preserved = membership.plan(first.view, {}, worktree='wt',
                                    generation='g3', raw_generation='raw-2',
                                    unknown=('b.md',))
        self.assertEqual(dict(preserved.view.members), {'b.md': h('B')})
        self.assertEqual(preserved.deleted, ('a.md',))

    def test_stale_cas_and_no_parent_resurrection(self):
        base = membership.plan(None, {'same.md': h('old')}, worktree='wt',
                               generation='g1', raw_generation='raw-1')
        active = membership.publish({}, base)
        deletion = membership.plan(base.view, {}, worktree='wt',
                                   generation='g2', raw_generation='raw-2')
        stale = membership.plan(base.view, {'same.md': h('stale')}, worktree='wt',
                                generation='g3', raw_generation='raw-3')
        active = membership.publish(active, deletion)
        self.assertEqual(active['wt'].members, ())
        self.assertIsNone(membership.publish(active, stale))
        self.assertEqual(dict(active['wt'].members), {})

    def test_reference_retention_including_pinned_old_view(self):
        a = membership.plan(None, {'a.md': h('shared')}, worktree='a',
                            generation='a1', raw_generation='r1').view
        b = membership.plan(None, {'b.md': h('shared')}, worktree='b',
                            generation='b1', raw_generation='r1').view
        a2 = membership.plan(a, {}, worktree='a', generation='a2',
                             raw_generation='r2').view
        self.assertEqual(membership.collectable_hashes({h('shared')}, [a2, b]), set())
        self.assertEqual(membership.collectable_hashes({h('shared')}, [a2]),
                         {h('shared')})
        self.assertEqual(membership.collectable_hashes({h('shared')}, [a2, a]), set())


class QuerySlice(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        query.create_schema(self.db)

    def stage(self, wt, gen, named_bodies):
        entries = {path: h(body) for path, (body, _) in named_bodies.items()}
        content = {h(body): (body, score) for body, score in named_bodies.values()}
        query.stage(self.db, wt, gen, entries, content)

    def test_scope_before_top_k_and_same_path_collision(self):
        self.stage('a', 'a1', {'same.md': ('A target', 1)})
        self.stage('b', 'b1', {'same.md': ('B decoy', 100),
                              'extra.md': ('B second', 90)})
        self.assertTrue(query.publish(self.db, 'a', None, 'a1'))
        self.assertTrue(query.publish(self.db, 'b', None, 'b1'))
        pin = query.pin_request(self.db, 'a')
        self.assertEqual([(r[0], r[2]) for r in query.search_pinned(self.db, pin, 1)],
                         [('same.md', 'A target')])
        self.assertNotEqual(query.search_pinned(self.db, pin, 1)[0][1], h('B decoy'))

    def test_bounded_global_overfetch_cannot_guarantee_a_collection_hit(self):
        # QMD 2.5.3 first bounds global FTS to limit*10 or vec to limit*3,
        # then applies its collection filter. This is the synthetic boundary.
        self.stage('a', 'a1', {'target.md': ('target', 0)})
        self.stage('b', 'b1', {f'decoy-{i}.md': (f'decoy {i}', i + 1)
                              for i in range(201)})
        self.assertTrue(query.publish(self.db, 'a', None, 'a1'))
        token = query.pin_request(self.db, 'a')
        for overfetch in (20 * 10, 20 * 3):
            global_first = self.db.execute('''
                WITH global_candidates AS (
                  SELECT m.worktree, m.path, c.score FROM membership m
                  JOIN content c ON c.hash=m.hash
                  ORDER BY c.score DESC LIMIT ?
                ) SELECT path FROM global_candidates WHERE worktree='a'
            ''', (overfetch,)).fetchall()
            self.assertEqual(global_first, [])
        self.assertEqual(query.search_pinned(self.db, token, 1)[0][0], 'target.md')

    def test_identical_content_reuses_one_hash_across_worktrees(self):
        self.stage('a', 'a1', {'a.md': ('same', 5)})
        self.stage('b', 'b1', {'b.md': ('same', 5)})
        self.assertEqual(self.db.execute('SELECT count(*) FROM content').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership').fetchone()[0], 2)
        self.assertEqual(query.collect_unreferenced(self.db), set())

    def test_stale_writer_cas_and_request_generation_pin(self):
        self.stage('wt', 'g1', {'old.md': ('old', 1)})
        self.stage('wt', 'g2', {'new.md': ('new', 2)})
        self.stage('wt', 'g3', {'late.md': ('late', 3)})
        self.assertTrue(query.publish(self.db, 'wt', None, 'g1'))
        pin = query.pin_request(self.db, 'wt')
        self.assertTrue(query.publish(self.db, 'wt', 'g1', 'g2'))
        self.assertFalse(query.publish(self.db, 'wt', 'g1', 'g3'))
        self.assertEqual([r[0] for r in query.search_pinned(self.db, pin, 2)],
                         ['old.md'])
        self.assertFalse(query.retire(self.db, 'wt', 'g1'))
        query.release_request(self.db, pin)
        self.assertTrue(query.retire(self.db, 'wt', 'g1'))
        self.assertEqual(query.collect_unreferenced(self.db), {h('old')})
        pin2 = query.pin_request(self.db, 'wt')
        self.assertEqual([r[0] for r in query.search_pinned(self.db, pin2, 2)],
                         ['new.md'])

    def test_complete_empty_generation_does_not_inherit_deleted_content(self):
        self.stage('wt', 'g1', {'deleted.md': ('deleted', 1)})
        self.stage('wt', 'g2', {})
        self.assertTrue(query.publish(self.db, 'wt', None, 'g1'))
        self.assertTrue(query.publish(self.db, 'wt', 'g1', 'g2'))
        self.assertFalse(query.publish(self.db, 'wt', 'g2', 'g1'))
        pin = query.pin_request(self.db, 'wt')
        self.assertEqual(query.search_pinned(self.db, pin, 10), [])
        self.assertEqual(query.collect_unreferenced(self.db), set())
        query.release_request(self.db, pin)
        self.assertTrue(query.retire(self.db, 'wt', 'g1'))
        self.assertEqual(query.collect_unreferenced(self.db), {h('deleted')})

    def test_missing_generation_and_duplicate_stage_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'view_not_published'):
            query.pin_request(self.db, 'missing')
        self.stage('wt', 'g1', {'x.md': ('x', 1)})
        with self.assertRaisesRegex(ValueError, 'generation_already_staged'):
            self.stage('wt', 'g1', {})
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'generation_not_staged'):
            query.publish(self.db, 'wt', None, 'missing')

    def test_plan_stage_publish_composition_and_old_pin(self):
        initial = membership.plan(None, {'same.md': h('old')}, worktree='wt',
                                  generation='g1', raw_generation='raw-1')
        self.assertTrue(query.stage_and_publish(self.db, initial,
                                                {h('old'): ('old', 1)}))
        old_pin = query.pin_request(self.db, 'wt')
        change = membership.plan(initial.view, {'same.md': h('new')},
                                 worktree='wt', generation='g2', raw_generation='raw-1')
        self.assertTrue(query.stage_and_publish(self.db, change,
                                                {h('new'): ('new', 2)}))
        self.assertEqual(query.search_pinned(self.db, old_pin, 1)[0][2], 'old')
        self.assertEqual(query.search_pinned(
            self.db, query.pin_request(self.db, 'wt'), 1)[0][2], 'new')
        stale = membership.plan(initial.view, {}, worktree='wt',
                                generation='g3', raw_generation='raw-2')
        self.assertFalse(query.stage_and_publish(self.db, stale, {}))
        self.assertEqual(query.search_pinned(
            self.db, query.pin_request(self.db, 'wt'), 1)[0][2], 'new')


if __name__ == '__main__':
    unittest.main()
