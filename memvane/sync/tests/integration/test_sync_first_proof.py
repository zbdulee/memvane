"""The selected sync pass writes the store only after its proofs.

The pass's first store write follows exactly one journal fence (the
selection epoch moves once per changed pass, never on an unchanged one); a
selection proof that moved between the route read and the writer scope stops
the pass before any store write or provider call; the after-sync drain
rechecks the proof under the owner.
"""
import unittest
from unittest import mock

from memvane.kernel.errors import IndexContractError
from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import SyncCase


class FirstWriteProof(SyncCase):
    def test_the_first_store_write_follows_one_journal_fence(self):
        epoch = self.epoch()
        self.card(self.CARDS[0][0], 'falconprobe')
        self.card(self.CARDS[1][0], 'hawkprobe')
        seen = []

        def ingest(real, *args, **kwargs):
            seen.append(self.epoch())
            return real(*args, **kwargs)
        with self.store_methods(ingest_attested_card=ingest):
            result = self.run_pass()
        self.assertEqual((result['state'], result['ingested']), ('indexed', 2), result)
        # Fenced before the first ingest, once for the whole pass (vector generation included).
        self.assertEqual(seen, [epoch + 1, epoch + 1])
        self.assertEqual(self.epoch(), epoch + 1)
        idle = self.run_pass()
        self.assertEqual((idle['state'], self.epoch()), ('unchanged', epoch + 1), idle)

    def test_a_moved_selection_proof_stops_the_pass_before_any_write(self):
        start, epoch = self.head(), self.epoch()
        self.card(self.CARDS[0][0], 'falconprobe')
        real = worker._selected

        def stale(project, **kwargs):
            decision, proof = real(project, **kwargs)
            return decision, dict(proof, selectionEpoch=proof['selectionEpoch'] + 7)
        writes = []
        with mock.patch.object(worker, '_selected', stale), self.provider_calls() as calls, \
                self.store_methods(ingest_attested_card=lambda real, *a, **k: writes.append(1) or real(*a, **k)):
            with self.assertRaises(IndexContractError) as caught:
                self.run_pass()
        self.assertEqual(str(caught.exception), 'selected_proof_changed')
        self.assertEqual((writes, calls), ([], []))
        self.assertEqual((self.head(), self.epoch()), (start, epoch))
        # The next pass with the current proof publishes.
        self.assertEqual(self.run_pass()['state'], 'indexed')

    def test_the_after_sync_drain_rechecks_the_proof_under_the_owner(self):
        self.card(self.CARDS[0][0], 'falconprobe')
        rechecks = []

        def drain(decision, *, recheck, owner=None):
            self.assertIsNotNone(owner)
            recheck()
            rechecks.append(1)
            return {'remaining': 0}
        with mock.patch('memvane.wiki.intake.api.drain', drain):
            result = self.run_pass()
        self.assertEqual((result['state'], rechecks), ('indexed', [1]), result)
        published = self.head()
        self.card(self.CARDS[1][0], 'hawkprobe')
        with mock.patch('memvane.wiki.intake.api.drain', drain), \
                mock.patch('memvane.journal.api.owned_selected_proof', lambda decision, owner: {'moved': True}):
            with self.assertRaises(IndexContractError) as caught:
                self.run_pass()
        self.assertEqual(str(caught.exception), 'selected_proof_changed')
        # The publish of that pass happened before the drain; the next pass is unchanged.
        self.assertNotEqual(self.head(), published)
        self.assertEqual(self.run_pass()['state'], 'unchanged')

    def test_remaining_external_edits_keep_the_wake_pending(self):
        with mock.patch('memvane.wiki.intake.api.drain', lambda decision, **kw: {'remaining': 3}):
            result = self.run_pass()
        self.assertEqual((result['state'], result['reason']), ('pending', 'external_edits_pending'), result)


if __name__ == '__main__':
    unittest.main()
