"""The selected sync pass over a minimal-settings project: wiki members, raw sources, refusals.

Only wiki cards are generation members; raw notes are the generation's
observed sources, so a raw edit alone republishes the sources without an
ingest. A provider-lane refusal defers only the vectors and keeps the
lexical publish.
"""
import unittest

from memvane.sync.tests.support import Coordinator, SyncCase, sha


class ProductSyncWorker(SyncCase):
    def observed(self, gen_id, path):
        """``(source_sha, byte_length, tracked_state, binding_state)`` of one generation source."""
        with self.db() as db:
            view_id, ordinal = self.at(db, gen_id)
            row = db.execute(
                'SELECT c.source_sha,c.byte_length,o.tracked_state,o.binding_state FROM s_sources s '
                'JOIN s_source_observations o ON o.observation_id=s.observation_id '
                'JOIN s_source_content c ON c.content_id=o.content_id '
                'WHERE s.view_id=? AND s.path=? AND s.gen_from<=? AND (s.gen_to IS NULL OR s.gen_to>?)',
                (view_id, path, ordinal, ordinal)).fetchone()
            return None if row is None else tuple(row)

    def test_wiki_cards_are_members_and_raw_notes_are_observed_sources(self):
        head = self.head()[0]
        self.assertEqual(sorted(self.members(head)), sorted(rel for rel, _token in self.CARDS))
        self.assertEqual(sorted(self.generation_sources(head)),
                         sorted('notes/' + token + '.md' for _rel, token in self.CARDS))
        raw = (self.project / 'notes/kestrelprobe.md').read_bytes()
        self.assertEqual(self.observed(head, 'notes/kestrelprobe.md'), (sha(raw), len(raw), 'tracked', 'observed'))

    def test_a_raw_edit_alone_republishes_the_sources_without_an_ingest(self):
        start = self.head()
        members = self.members(start[0])
        note = self.fixture.write_note('notes/plover-extra.md', 'A raw note the next generation observes.\n')
        edited = self.run_pass()
        self.assertEqual((edited['state'], edited['ingested'], edited['removed']), ('indexed', 0, 0), edited)
        self.assertEqual(edited['sources'], len(self.CARDS) + 1)
        self.assertEqual(self.members(edited['generation']), members)
        raw = note.read_bytes()
        self.assertEqual(self.observed(edited['generation'], 'notes/plover-extra.md'),
                         (sha(raw), len(raw), 'untracked', 'observed'))
        self.assertIsNone(self.observed(start[0], 'notes/plover-extra.md'))
        idle = self.run_pass()
        self.assertEqual(idle['state'], 'unchanged', idle)

    def test_a_provider_lane_refusal_defers_only_the_vectors(self):
        self.card(self.CARDS[0][0], 'falconprobe')
        refused = self.run_pass(Coordinator(refuse='learning_deferred_resource'))
        self.assertEqual((refused['state'], refused['reason'], refused['ingested']),
                         ('deferred', 'learning_deferred_resource', 1), refused)
        self.assertEqual(self.head()[0], refused['generation'])
        self.assertEqual(self.coverage(refused['generation'])[1], 'lexical')

    # Product regression candidate (reported to the lead): a provider-lane refusal
    # keeps the coordinator's admission criterion (``report['reason']``) only for an
    # owner-level deferral (``_deferred``); the vector deferral drops it.
    @unittest.expectedFailure
    def test_a_provider_lane_refusal_keeps_its_admission_criterion(self):
        from memvane.kernel.resource_deferred import ResourceDeferred

        class Criterion(Coordinator):
            def activity(self, lane, policy, report):
                raise ResourceDeferred('learning_deferred_resource', {'ready': False, 'reason': 'memory_reserve'})
        self.card(self.CARDS[0][0], 'falconprobe')
        refused = self.run_pass(Criterion())
        self.assertIn('memory_reserve', refused.get('detail', ''), refused)


if __name__ == '__main__':
    unittest.main()
