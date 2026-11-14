"""Consumers of the published view head: wiki discovery after the pass and dedup retrieval.

The pass hands discovery (``wiki.intake.api.drain_after_sync``) the
generation it published and the raw snapshot of its own source scan, so one
raw scan serves the manifest sources and discovery; a discovery retry defers
the wake with a fixed reason and a blocked discovery is reported in the
result. The retroactive dedup scan reads the view head's vectors.
"""
import unittest
from unittest import mock

from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import SyncCase
from memvane.wiki.intake import api as intake
from tests.support.settings_fake import expanded, use_config


class ViewConsumers(SyncCase):
    def grant(self):
        decision = self.target.decision
        grant = intake.record_grant(decision, 'sync', engine='codex')
        self.assertTrue(intake.activate_grant(decision, 'sync', grant))

    def test_one_raw_scan_serves_the_manifest_sources_and_discovery(self):
        self.grant()
        self.fixture.write_note('notes/discovered.md', 'A raw note discovery must see.\n')
        scans, handed = [], []
        real_scan, real_drain = worker._schema3_sources, intake.drain_after_sync

        def scan(decision, handle, deadline, *, raw_scan=None):
            observed = real_scan(decision, handle, deadline, raw_scan=raw_scan)
            scans.append(raw_scan.get('snapshot') if raw_scan is not None else None)
            return observed

        def drain(decision, observed, **kwargs):
            handed.append(kwargs)
            return real_drain(decision, observed, **kwargs)
        with mock.patch.object(worker, '_schema3_sources', scan), \
                mock.patch('memvane.wiki.intake.api.drain_after_sync', drain):
            result = self.run_pass()
        self.assertEqual((result['state'], result['ingested']), ('indexed', 0), result)
        self.assertNotEqual(result.get('wikiDiscovery', {}).get('state'), 'blocked', result)
        self.assertEqual(len(scans), 1)
        self.assertEqual(len(handed), 1)
        self.assertEqual(handed[0]['view_generation'], result['generation'])
        self.assertEqual(handed[0]['view_generation'], self.head()[0])
        self.assertIs(handed[0]['raw_snapshot'], scans[0])
        self.assertTrue(any(key.endswith('/notes/discovered.md') for key in scans[0]), sorted(scans[0]))
        # The raw note is a manifest source of the generation, not a member.
        self.assertIn('notes/discovered.md', self.generation_sources(result['generation']))
        self.assertNotIn('notes/discovered.md', self.members(result['generation']))

    def test_a_discovery_retry_defers_and_a_blocked_discovery_is_reported(self):
        self.grant()
        self.card(self.CARDS[0][0], 'falconprobe')
        with mock.patch('memvane.wiki.intake.api.drain_after_sync',
                        lambda decision, observed, **kw: {'state': 'retry', 'reason': 'state_busy'}):
            retry = self.run_pass()
        self.assertEqual(retry, {'state': 'deferred', 'reason': 'wiki_discovery_state_busy',
                                 'generation': self.head()[0]})
        blocked_outcome = {'state': 'blocked', 'reason': 'grant_revoked'}
        with mock.patch('memvane.wiki.intake.api.drain_after_sync', lambda decision, observed, **kw: blocked_outcome):
            blocked = self.run_pass()
        self.assertEqual((blocked['state'], blocked['wikiDiscovery']), ('unchanged', blocked_outcome), blocked)
        with mock.patch('memvane.wiki.intake.api.drain_after_sync',
                        lambda decision, observed, **kw: {'state': 'complete'}):
            quiet = self.run_pass()
        self.assertNotIn('wikiDiscovery', quiet)

    def test_the_dedup_scan_reads_the_view_head_vectors(self):
        from memvane.wiki.dedup import api as dedup
        # Two cards with the same bytes: one blob, one embedding set, cosine 1.
        (self.wiki / 'concepts/kestrel-twin.md').write_bytes((self.wiki / self.CARDS[3][0]).read_bytes())
        result = self.run_pass()
        self.assertEqual(result['vector']['state'], 'published', result)
        use_config(self, self.fixture, expanded(self.fixture, compile={'mode': 'guarded'}))
        scanned = dedup.run(str(self.project))
        self.assertEqual(scanned['state'], 'scanned', scanned)
        self.assertEqual(scanned['cards'], len(self.members(result['generation'])), scanned)
        self.assertGreaterEqual(scanned['queued'], 1, scanned)


if __name__ == '__main__':
    unittest.main()
