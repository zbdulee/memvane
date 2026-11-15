"""Linked worktree and branch views: the main checkout's owner publishes each view on its own.

A linked Git worktree of the selected project (same settings identity) gets
its own view, registered and published by the main checkout's pass after the
main view; an edit in one checkout moves only that view; a new branch is a
cold view whose cards are ingested again (trust is never reused across
views). A checkout whose settings identity differs is not a linked view.
"""
import json
import unittest

from memvane.store.types import BlobStoreError
from memvane.sync.tests.support import SyncCase
from tests.support.project import settle_index


class WorktreeViews(SyncCase):
    def setUp(self):
        super().setUp()
        self.linked = self.fixture.linked_worktree('side', 'side')

    def linked_card(self, rel, token):
        """A card and its raw note written only in the linked worktree."""
        return self.card(rel, token, project=self.linked)

    def recall_at(self, root, prompt):
        """``(route state, context, recall event)`` of one in-process recall from ``root``."""
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        decision = select_route(str(root), action='recall')
        served = serve_selected('recall', 'claude', decision, {
            'session_id': 'fixture-session', 'cwd': str(root), 'hook_event_name': 'UserPromptSubmit',
            'prompt': prompt})
        context = ((served or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
        recalls = self.events('recall')
        return decision.get('state'), context, (recalls[-1] if recalls else {})

    def linked_rows(self):
        with self.db() as db:
            return [row[0] for row in db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                                                 (str(self.linked.root),))]

    def trust(self, gen_id):
        with self.db() as db:
            view_id, ordinal = self.at(db, gen_id)
            return dict(db.execute('SELECT path,trust_digest FROM s_cards WHERE view_id=? AND gen_from<=? '
                                   'AND (gen_to IS NULL OR gen_to>?)', (view_id, ordinal, ordinal)))

    def built(self):
        """One pass publishing main (unchanged) and the linked view with its own card."""
        self.linked_card('concepts/wren-linked.md', 'wrenlinkedprobe')
        with self.provider_calls() as calls:
            first = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        self.assertEqual([item['root'] for item in first['worktrees']], [str(self.linked.root)])
        item = first['worktrees'][0]
        self.assertEqual(item['state'], 'indexed', first)
        return first, item, calls

    # -- registration and isolation ----------------------------------------------
    def test_linked_view_is_registered_published_and_read_on_its_own(self):
        main_gen = self.head()[0]
        state, context, event = self.recall_at(self.linked.root, 'wrenlinkedprobe')
        self.assertEqual((state, context), ('worktree', ''))
        self.assertIn('laya_view_preparing', event.get('notes') or [], event)
        self.assertEqual(self.linked_rows(), [])
        first, item, calls = self.built()
        # Main had nothing to ingest; the linked view is a new view: every card is ingested.
        self.assertEqual(first['generation'], main_gen)
        self.assertEqual(len(self.linked_rows()), 1)
        expected = sorted(list(self.members(main_gen)) + ['concepts/wren-linked.md'])
        self.assertEqual(item['ingested'], len(expected))
        self.assertEqual(sorted(self.members(item['generation'])), expected)
        self.assertNotEqual(self.view_of(item['generation']), self.view_of(main_gen))
        # Content the two views share is embedded once: only the linked-only card asks the provider.
        self.assertEqual([kind for kind, _path in calls], ['chunks'])
        self.assertEqual(item['vector']['state'], 'published', item)
        # Each checkout answers from its own view.
        state, own, _event = self.recall_at(self.linked.root, 'wrenlinkedprobe')
        self.assertEqual(state, 'worktree')
        self.assertIn('generation ' + item['generation'], own)
        self.assertIn('concepts/wren-linked.md', own)
        _state, main_only, _event = self.recall_at(self.project, 'wrenlinkedprobe')
        self.assertNotIn('concepts/wren-linked.md', main_only)
        self.assertNotIn(item['generation'], main_only)

    def test_an_edit_in_one_checkout_leaves_the_other_view_unchanged(self):
        first, item, _calls = self.built()
        main_gen, linked_view = first['generation'], self.view_of(item['generation'])
        self.linked_card('concepts/kestrel-perch.md', 'ospreylinkedprobe')
        edited = self.run_pass()
        self.assertEqual((edited['generation'], edited['worktrees'][0]['ingested']), (main_gen, 1), edited)
        moved = self.head(linked_view)[0]
        self.assertNotEqual(moved, item['generation'])
        self.assertEqual(self.changed_paths(self.members(item['generation']), self.members(moved)),
                         ['concepts/kestrel-perch.md'])
        self.card('concepts/heron-main.md', 'heronmainprobe')
        main_edit = self.run_pass()
        self.assertEqual(main_edit['ingested'], 1, main_edit)
        self.assertNotEqual(main_edit['generation'], main_gen)
        self.assertEqual(main_edit['worktrees'][0]['state'], 'unchanged', main_edit)
        self.assertEqual(self.head(linked_view)[0], moved)

    def test_a_linked_branch_switch_is_a_cold_view_with_no_trust_reuse(self):
        _first, item, _calls = self.built()
        side_view = self.view_of(item['generation'])
        self.linked.git('switch', '-q', '-c', 'other')
        settle_index(self.linked.root)
        with self.provider_calls() as calls:
            switched = self.run_pass()
        other = switched['worktrees'][0]
        self.assertEqual(other['state'], 'indexed', switched)
        self.assertEqual(other['ingested'], len(self.members(item['generation'])))
        self.assertNotEqual(self.view_of(other['generation']), side_view)
        self.assertEqual(self.head(side_view)[0], item['generation'])
        # Same bytes, new trust rows: the content sets are reused, no provider call.
        before, after = self.trust(item['generation']), self.trust(other['generation'])
        self.assertEqual(sorted(before), sorted(after))
        self.assertTrue(all(after[path] != before[path] for path in before if before[path] is not None))
        self.assertEqual(calls, [])

    def test_a_main_branch_switch_is_a_cold_view_too(self):
        main_gen = self.head()[0]
        self.fixture.git('switch', '-q', '-c', 'feature')
        settle_index(self.project)
        _state, cold, event = self.recall_at(self.project, 'kestrelprobe')
        self.assertEqual(cold, '')
        self.assertIn('laya_view_preparing', event.get('notes') or [], event)
        built = self.run_pass()
        self.assertEqual((built['state'], built['ingested']), ('indexed', len(self.members(main_gen))), built)
        self.assertNotEqual(self.view_of(built['generation']), self.view_of(main_gen))
        self.assertEqual(self.head(self.view_of(main_gen))[0], main_gen)
        before, after = self.trust(main_gen), self.trust(built['generation'])
        self.assertTrue(all(after[path] != before[path] for path in before))
        _state, answer, _event = self.recall_at(self.project, 'kestrelprobe')
        self.assertIn('generation ' + built['generation'], answer)

    # -- held and unclassified checkouts --------------------------------------------
    def test_a_refused_linked_ingest_holds_that_view_and_the_main_pass_goes_on(self):
        self.linked_card('concepts/wren-linked.md', 'wrenlinkedprobe')
        self.card('concepts/heron-main.md', 'heronmainprobe')
        linked = str(self.linked.root)

        def refusing(real, worktree_id, path, *, decision, **kwargs):
            if decision['projectRoot'] == linked:
                raise BlobStoreError('attestation_decision_mismatch')
            return real(worktree_id, path, decision=decision, **kwargs)
        with self.store_methods(ingest_attested_card=refusing):
            result = self.run_pass()
        self.assertEqual((result['state'], result['ingested']), ('indexed', 1), result)
        self.assertEqual({k: result['worktrees'][0][k] for k in ('state', 'reason')},
                         {'state': 'held', 'reason': 'attestation_decision_mismatch'})
        self.assertIn('concepts/heron-main.md', self.members(result['generation']))
        retried = self.run_pass()
        self.assertEqual(retried['worktrees'][0]['state'], 'indexed', retried)

    def test_a_checkout_with_another_settings_identity_is_not_a_linked_view(self):
        value = json.loads(self.linked.settings_path.read_text(encoding='utf8'))
        self.linked.settings_path.write_text(json.dumps(dict(value, name=value['name'] + '-other')),
                                             encoding='utf8')
        from memvane.journal.api import select_route
        self.assertNotEqual(select_route(str(self.linked.root), action='recall').get('state'), 'worktree')
        self.linked_card('concepts/wren-linked.md', 'wrenlinkedprobe')
        result = self.run_pass()
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertNotIn('worktrees', result)
        self.assertEqual(self.linked_rows(), [])


if __name__ == '__main__':
    unittest.main()
