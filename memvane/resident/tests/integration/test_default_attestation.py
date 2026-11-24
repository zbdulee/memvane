"""Classify the selected journal's full provider proof before a worker starts."""
import errno
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from memvane.kernel import bundle_paths
from memvane.journal import api as journal
from memvane.provider import api as provider
from memvane.resident import embed_resident as owner, resident_service as svc
from tests.support.attested_install import AttestedInstall


class DefaultAttestation(AttestedInstall):
    def setUp(self):
        super().setUp()
        self.worker = svc.Worker(svc._worker_key(self.manifest), str(self.home / 'w.sock'))
        self.start_worker = Mock(return_value=object())
        self.service = svc.Service(self.home, bundle={'version': '1.1.1'}, start_worker=self.start_worker, admit=lambda: True)
        self.assertIs(self.service.attest, svc.default_attest)

    def attest(self):
        self.service._load_attested(str(self.project.root), self.decision, self.proof, 'selected',
                                    self.proof['providerManifest'], self.worker, 'seal')
        return owner.read_owner_state(self.proof['providerManifest'])

    def assert_failure(self, state, terminal, code):
        self.start_worker.assert_not_called()
        self.assertEqual((state['reason'], state['terminal'], state['attempts']),
                         ('attestation_invalid', True, 3) if terminal else ('startup_failed', False, 1))
        self.assertEqual(self.install.events()[-1]['reason'], code)

    def test_model_bytes_mismatch_is_terminal(self):
        Path(self.manifest['modelPath']).write_bytes(b'changed fixture')
        self.assert_failure(self.attest(), True, 'provider_identity_changed')

    def test_damaged_install_before_first_proof_backs_off_without_writing_owner_state(self):
        project = str(self.project.root)
        Path(self.manifest['modelPath']).write_bytes(b'changed before adoption')
        svc.register(project, root=self.home)
        with patch.object(self.service, 'prove', wraps=self.service.prove) as prove, \
             patch.object(svc.time, 'monotonic') as clock:
            for at in (100, 105, 399):
                clock.return_value = at
                self.service.tick()
            self.assertEqual(prove.call_count, 1)
            self.assertEqual(self.service.retry[project], 400)
        self.assertIsNone(owner.read_owner_state(self.manifest))
        self.start_worker.assert_not_called()

    @contextmanager
    def changed_bundle(self):
        # Resolve both artifact checks against a new bundle whose provider bytes lack approval.
        bundle = self.home / 'other-bundle'
        for rel in (bundle_paths.WORKER_RELATIVE_PATH, bundle_paths.PROVIDER_RELATIVE_PATH, bundle_paths.CHUNKER_RELATIVE_PATH):
            path = bundle / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundle_paths.bundle_file(rel), path)
        with (bundle / bundle_paths.PROVIDER_RELATIVE_PATH).open('ab') as target:
            target.write(b'\n# changed bytes\n')
        with patch.object(bundle_paths, 'plugin_root', return_value=bundle):
            yield

    def test_unapproved_bundle_is_terminal(self):
        with self.changed_bundle():
            self.assert_failure(self.attest(), True, 'provider_identity_changed')

    def test_ledger_io_is_retryable_and_repaired_proof_starts(self):
        # Register the original pair and then the changed bundle under the same embedding identity.
        provider.reattestation_update(self.state, self.manifest)
        real_open = os.open
        @contextmanager
        def ledger_fault(fail_at=None):
            reads = []
            def open_ledger(path, *args, **kwargs):
                if Path(path) == self.state / 'provider-reattest-pairs.json':
                    reads.append(path)
                    if len(reads) == fail_at:
                        raise OSError(errno.EIO, 'fixture ledger read')
                return real_open(path, *args, **kwargs)
            with patch.object(os, 'open', side_effect=open_ledger):
                yield reads
        with self.changed_bundle():
            provider.reattestation_update(self.state, self.manifest)
            self.proof = journal.verify_selected_route_for_write(self.decision)
            with ledger_fault(1):
                self.assertFalse(journal.verify_selected_route(self.decision)['providerAvailable'])
            with ledger_fault() as healthy:
                svc.default_attest(self.decision, self.proof, 'selected')
            self.assertGreaterEqual(len(healthy), 3)
            for fail_at in range(1, len(healthy) + 1):
                with self.subTest(fail_at=fail_at), ledger_fault(fail_at):
                    self.assert_failure(self.attest(), False, 'worker_attestation_unavailable')
            self.attest()
            self.start_worker.assert_called_once()

    def test_fifo_attestation_is_rejected_before_a_blocking_read(self):
        path = self.state / 'provider-attestation.json'
        path.unlink()
        os.mkfifo(path, 0o600)
        opening = os.open
        def open_file(name, flags, *args, **kwargs):
            if Path(name) == path:
                self.assertTrue(flags & os.O_NONBLOCK, 'opening this FIFO would block adoption')
            return opening(name, flags, *args, **kwargs)
        with patch.object(os, 'open', side_effect=open_file):
            self.assert_failure(self.attest(), True, 'unsafe_production_attestation')

    def test_invalid_attestation_json_is_terminal(self):
        for raw in (b'{broken-json', b'{"sha":0,"sha":1}', b'{"n":NaN}', br'{"value":"\ud800"}'):
            with self.subTest(raw=raw):
                (self.state / 'provider-attestation.json').write_bytes(raw)
                self.assert_failure(self.attest(), True, 'production_attestation_invalid')

    def test_journal_refuses_an_explicitly_unapproved_worker(self):
        from memvane.journal.types import JournalEvidenceError
        proof = dict(self.proof, productRouteAllowed=False)
        with self.assertRaisesRegex(JournalEvidenceError, 'worker_provider_unapproved') as refused:
            journal.worker_provider_manifest(self.decision, proof)
        self.service._attest_failed(str(self.project.root), self.decision, self.manifest, refused.exception, 0)
        self.assert_failure(owner.read_owner_state(self.manifest), True, 'worker_provider_unapproved')

    def test_generation_directory_io_fails_before_comparing_the_inventory(self):
        real_scan = os.scandir
        def stalled(directory):
            if isinstance(directory, (str, Path)) and Path(directory) == Path(self.manifest['modelPath']).parent:
                raise OSError(errno.EIO, 'fixture directory enumeration')
            return real_scan(directory)
        with patch.object(os, 'scandir', side_effect=stalled):
            self.assert_failure(self.attest(), False, 'worker_attestation_unavailable')

    def test_wrapped_io_and_file_moving_during_hash_are_retryable(self):
        from memvane.kernel.errors import IndexContractError
        pending = provider.RuntimeUnavailable('laya_embed_runtime_corrupt')
        pending.__cause__ = OSError(errno.EIO, 'fixture generation read')
        for port, error in (('verify_attested_files', pending), ('runtime_file_sha256', IndexContractError('provider_artifact_changed'))):
            with self.subTest(port=port), patch.object(provider, port, side_effect=error):
                self.assert_failure(self.attest(), False, 'worker_attestation_unavailable')

    def test_selection_change_defers_without_starting(self):
        self.proof = dict(self.proof, scopeId='different')
        self.assertIsNone(self.attest())
        self.start_worker.assert_not_called()
        self.assertEqual(self.install.events()[-1]['reason'], 'worker_selection_changed')
