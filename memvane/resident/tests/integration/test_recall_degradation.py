"""Resident deferrals leave recall on its lexical-safe path."""
from unittest.mock import patch

from memvane.journal import api as journal_api
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.recall import api as recall_api
from memvane.resident import api as resident_api
from memvane.runtime import api as runtime_api
from tests.support.case import FixtureCase


class RecallDegradation(FixtureCase):
    def test_protocol_mismatch_and_cooldown_degrade_without_failure(self):
        decision = {'projectRoot': str(self.home / 'project')}
        proof = {'providerManifest': {'serviceSocketPath': str(self.home / 'absent.sock')},
                 'selectionEpoch': 1, 'writerFloor': '1.0.0'}
        cases = (('starting', 'laya_service_protocol_mismatch', 'selected_provider_protocol_mismatch'),
                 ('starting', 'laya_service_preparing', 'selected_provider_preparing'),
                 ('startup_failed', 'laya_service_backoff', 'selected_provider_retry_wait'))
        with patch.object(journal_api, 'verify_selected_route', return_value=proof), \
             patch.object(runtime_api, 'request', return_value={'status': 'queued'}) as wake, \
             patch.object(resident_api, 'failure_decision', return_value=None), \
             patch.object(resident_api, 'read_owner_state') as read, \
             patch.object(resident_api, 'ensure_running') as ensure:
            for reason, deferred, expected in cases:
                with self.subTest(reason=reason, deferred=deferred):
                    state = {'reason': reason, 'terminal': False, 'attempts': 2}
                    read.return_value = state
                    ensure.side_effect = ResourceDeferred(deferred)
                    self.assertEqual(recall_api.wake_provider(decision, proof), expected)
                    self.assertEqual(state['attempts'], 2)
        self.assertEqual(wake.call_count, len(cases))
