"""Source intent planning keeps trigger consent, order and revision coalescing."""
import copy

from memvane.wiki.intake import wiki_compile_enqueue as E
from memvane.wiki.intake import wiki_source_intent as I
from tests.support.case import FixtureCase


class PurePlanning(FixtureCase):
    def test_trigger_policy_keeps_consent_per_caller(self):
        self.assertEqual(I.trigger_policy(False),
                         (('post_sync_source', 'post_tool_source'), 'post_sync_source'))
        # SessionStart accepts only its own grant; post_tool consent is not widened.
        self.assertEqual(I.trigger_policy(True), (('session_start_source',), 'session_start_source'))

    def test_enqueue_module_aliases_are_the_same_labels(self):
        self.assertEqual(E.POST_TOOL_TRIGGER, 'post_tool_source')
        self.assertEqual(E.COMPILE_TRIGGER, 'post_sync_source')
        self.assertEqual(E.COMPILE_ACCEPTED_TRIGGERS, ('post_sync_source', 'post_tool_source'))

    def test_changed_paths_are_sorted_deduplicated_and_skip_deletes(self):
        changed = {
            'zeta': {'root': '/w/z', 'created': ['b.md'], 'updated': ['a.md', 'b.md'], 'deleted': ['c.md']},
            'alpha': {'root': '/w/a', 'created': [], 'updated': [], 'deleted': ['gone.md']},
            'mid': {'root': '/w/m', 'created': ['y.md', 'x.md'], 'updated': [], 'deleted': []},
        }
        before = copy.deepcopy(changed)
        self.assertEqual(list(I.changed_source_paths(changed)), [
            ('mid', '/w/m', 'x.md'), ('mid', '/w/m', 'y.md'),
            ('zeta', '/w/z', 'a.md'), ('zeta', '/w/z', 'b.md')])
        self.assertEqual(changed, before)

    def test_plan_splits_by_cap_in_candidate_order(self):
        rows = [('c', f'{i}.md', {'n': i}) for i in range(3)]
        cases = {
            50: ([0, 1, 2], [], 'queued'),
            3: ([0, 1, 2], [], 'queued'),
            2: ([0, 1], [('c', '2.md')], 'capped'),
            0: ([], [('c', '0.md'), ('c', '1.md'), ('c', '2.md')], 'capped'),
        }
        for cap, (picked, deferred, reason) in cases.items():
            plan = I.plan_enqueue(iter(rows), cap)
            self.assertEqual([r['n'] for r in plan['records']], picked, cap)
            self.assertEqual(plan['result'], {'queued': len(picked), 'deferred': len(deferred),
                                              'deferredFiles': deferred, 'reason': reason}, cap)
            # Records are passed through, not copied or rewritten.
            for record in plan['records']:
                self.assertIs(record, rows[record['n']][2])

    def test_empty_plan_and_result_shape(self):
        self.assertEqual(I.plan_enqueue([], 5),
                         {'records': [], 'result': {'queued': 0, 'deferred': 0,
                                                    'deferredFiles': [], 'reason': 'no_sources'}})
        first, second = I.enqueue_result('x'), I.enqueue_result('x')
        self.assertIsNot(first['deferredFiles'], second['deferredFiles'])

    def test_intent_identity_is_the_hook_duplicate_key(self):
        record = {'cwd': '/p', 'trigger': 't', 'engine': 'e', 'ts': 'now',
                  'source': {'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs'}}
        self.assertEqual(I.intent_identity(record), ('/p', 'docs/a.md', 'docs'))

    def test_revision_key_keeps_trigger_and_ignores_the_owner_path(self):
        def row(cwd, trigger, **extra):
            return dict({'cwd': cwd, 'trigger': trigger, 'engine': 'e', 'ts': 'now',
                         'source': {'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs'}},
                        **extra)
        revised = {'contentRevision': 'r1'}
        # The queue is project-owned; a respelled root must not split one intent.
        self.assertEqual(I.revision_key(row('/p', 'session_start_source', **revised)),
                         I.revision_key(row('/private/p', 'session_start_source', **revised)))
        self.assertNotEqual(I.revision_key(row('/p', 'session_start_source', **revised)),
                            I.revision_key(row('/p', 'post_sync_source', **revised)))
        # PostToolUse, manual sync and recovery rows have no revision, hence no key.
        self.assertIsNone(I.revision_key(row('/p', 'post_tool_source')))
        self.assertIsNone(I.revision_key(row('/p', 'session_start_source', contentRevision='')))
        self.assertIsNone(I.revision_key(['not', 'a', 'row']))

