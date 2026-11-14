"""Resident retry state and admission under a controlled clock."""
from tests.support.case import FixtureCase
from unittest.mock import patch
from memvane.resident import embed_resident as owner
from memvane.resident import resident_service as svc
from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred


class RecoveryChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = self.home / 'r'
        self.base.mkdir(mode=0o700)
        self.manifest = {'serviceSocketPath': str(self.base / 'embed.sock'),
                         'embeddingFingerprint': 'a' * 64,
                         'approvedProviderId': 'synthetic',
                         'providerArtifactSha256': 'b' * 64,
                         'modelId': 'synthetic-model',
                         'dimension': 4}

    def stamp(self, reason, *, terminal=True, attempts=3):
        with patch.object(owner.time, 'time', return_value=1000):
            owner._write_state(self.manifest, reason, terminal=terminal, attempts=attempts)

    def cold(self, now):
        return (patch.object(owner, 'probe', return_value='absent'),
                patch.object(owner.time, 'time', return_value=now))

    def test_legacy_transient_terminal_recovers_after_cooldown(self):
        for reason in ('startup_failed', 'worker_exited'):
            with self.subTest(reason=reason):
                self.stamp(reason)
                probe, clock = self.cold(1301)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                        owner.ensure_running('/synthetic/project', self.manifest)
                adopt.assert_called_once()

    def test_transient_terminal_waits_without_extending_cooldown(self):
        self.stamp('startup_failed')
        probe, clock = self.cold(1299)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()
        self.assertEqual(owner.read_owner_state(self.manifest)['at'], 1000)

    def test_security_terminal_never_expires(self):
        for reason in ('attestation_invalid', 'identity_invalid'):
            with self.subTest(reason=reason):
                self.stamp(reason)
                probe, clock = self.cold(100000)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                        owner.ensure_running('/synthetic/project', self.manifest)
                adopt.assert_not_called()

    def test_abrupt_third_launch_enters_recoverable_cooldown(self):
        self.stamp('ready', terminal=False)
        probe, clock = self.cold(1021)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()
        probe, clock = self.cold(1322)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_called_once()
