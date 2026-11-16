"""A failed reattestation does not spawn an unbounded worker stream."""
import unittest
from unittest import mock
from memvane.host import provider_recovery


class ProviderRecovery(unittest.TestCase):
    def test_template_difference_never_offers_a_wake(self):
        proof = {'journalPath':'/synthetic/journal', 'providerManifest':{}}
        decision = {'projectRoot':'/synthetic/project'}
        with mock.patch('memvane.diagnostics.api.build_progress_events', return_value=[]), \
             mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.8'), \
             mock.patch('memvane.provider.api.reattestation_differences', return_value=['queryTemplate']), \
             mock.patch('memvane.runtime.api.request') as request:
            self.assertFalse(provider_recovery.offer('recall', decision, proof, {}, allowed=True, successful=True))
        request.assert_not_called()

    def test_two_rejections_hold_offer_for_same_version(self):
        proof = {'journalPath':'/synthetic/journal', 'providerManifest':{}}
        decision = {'projectRoot':'/synthetic/project'}
        rejected = {'event':'provider.reattest', 'outcome':'rejected', 'pluginVersion':'1.0.8'}
        with mock.patch('memvane.diagnostics.api.build_progress_events', return_value=[rejected, rejected]), \
             mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.8'), \
             mock.patch('memvane.provider.api.reattestation_differences', return_value=[]), \
             mock.patch('memvane.runtime.api.request') as request:
            for _ in range(20):
                self.assertFalse(provider_recovery.offer('recall', decision, proof, {}, allowed=True, successful=True))
        request.assert_not_called()

    def test_accepted_event_or_new_version_allows_offer(self):
        proof = {'journalPath':'/synthetic/journal', 'providerManifest':{}}
        decision = {'projectRoot':'/synthetic/project'}
        rejected = {'event':'provider.reattest', 'outcome':'rejected', 'pluginVersion':'1.0.8'}
        accepted = {'event':'provider.reattest', 'outcome':'accepted', 'pluginVersion':'1.0.8'}
        with mock.patch('memvane.diagnostics.api.build_progress_events', return_value=[rejected, rejected, accepted]), \
             mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.8') as version, \
             mock.patch('memvane.provider.api.reattestation_differences', return_value=[]), \
             mock.patch('memvane.runtime.api.request', return_value={'status':'queued'}) as request:
            self.assertTrue(provider_recovery.offer('recall', decision, proof, {}, allowed=True, successful=True))
            version.return_value = '1.0.9'
            self.assertTrue(provider_recovery.offer('recall', decision, proof, {}, allowed=True, successful=True))
        self.assertEqual(request.call_count, 2)
