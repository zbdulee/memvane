"""Read-only hooks over the selected installation's actual provider proof."""
import errno
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from memvane.host import host_response
from memvane.journal import api as journal
from memvane.provider import api as provider
from tests.support.attested_install import AttestedInstall, encoded


class ReadProofHooks(AttestedInstall):
    def run_hook(self, action, host='claude', *, malformed=False):
        project = str(self.project.root)
        payload = {'cwd': project, 'hook_event_name': 'UserPromptSubmit', 'prompt': 'synthetic'}
        if action == 'gate':
            payload = {'cwd': project, 'hook_event_name': 'PreToolUse', 'tool_name': 'Edit',
                       'tool_input': {} if malformed else {'file_path': str(self.project.root / 'notes.md')}}
        stdout, stderr = io.StringIO(), io.StringIO()
        with io.TextIOWrapper(io.BytesIO(encoded(payload))) as stdin, patch.object(sys, 'stdin', stdin), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            status = host_response.main(['serve-stdin', action, host, project])
        return status, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def test_read_hooks_handle_model_io_then_recover(self):
        from memvane.journal.types import JournalEvidenceError
        model = self.manifest['modelPath']
        real_hash = provider.runtime_file_sha256
        def hash_file(path):
            if path == model:
                raise OSError(errno.EIO, 'fixture model read')
            return real_hash(path)

        empty_recall = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': ''}}
        for action, expected in (('recall', empty_recall), ('gate', {})):
            with self.subTest(action=action):
                with patch.object(provider, 'runtime_file_sha256', side_effect=hash_file) as read:
                    status, payload, err = self.run_hook(action)
                    read.assert_any_call(model)
                    self.assertEqual((status, payload), (0, expected), err)
                    self.assertEqual(self.install.events()[-1]['reason'], 'worker_attestation_unavailable')
                    with self.assertRaisesRegex(JournalEvidenceError, '^worker_attestation_unavailable$'):
                        journal.verify_selected_route_for_write(self.decision)
                # Admit the synthetic query independently of host pressure. Files must prove again.
                with patch('memvane.resident.api.admit_query'), \
                     patch.object(provider, 'runtime_file_sha256', wraps=real_hash) as recovered, \
                     patch.object(provider, 'query_vectors', return_value=([[1.0, 0.0]], 1)):
                    status, payload, err = self.run_hook(action)
                    recovered.assert_any_call(model)
                self.assertEqual(status, 0, err)
                if action == 'recall':
                    self.assertIn('Synthetic', payload['hookSpecificOutput']['additionalContext'])
                else:
                    self.assertEqual(payload, {})
                self.assertNotEqual(self.install.events()[-1].get('reason'), 'worker_attestation_unavailable')

    def test_gate_validates_edit_input_even_when_model_proof_is_unavailable(self):
        real_hash = provider.runtime_file_sha256
        for host in ('claude', 'codex'):
            for faulty in (False, True):
                def hash_file(path):
                    if faulty and path == self.manifest['modelPath']:
                        raise OSError(errno.EIO, 'fixture model read')
                    return real_hash(path)
                with self.subTest(host=host, faulty=faulty), patch.object(provider, 'runtime_file_sha256',
                                                                         side_effect=hash_file) as read:
                    self.assertEqual(self.run_hook('gate', host), (0, {}, ''))
                    status, payload, err = self.run_hook('gate', host, malformed=True)
                    read.assert_any_call(self.manifest['modelPath'])
                    self.assertEqual((status, payload), (3, None))
                    self.assertIn('edit_path_missing', err)

    def test_hook_refuses_a_provider_query_with_changed_identity(self):
        from memvane.kernel.errors import IndexContractError
        with patch('memvane.resident.api.admit_query'), \
             patch.object(provider, 'query_vectors', side_effect=IndexContractError('provider_identity_mismatch')) as query:
            status, payload, err = self.run_hook('recall')
        query.assert_called_once()
        self.assertEqual((status, payload), (3, None))
        self.assertIn('selected_provider_identity_transition', err)
        self.assertEqual(self.install.events()[-1]['outcome'], 'refused')

    def test_read_hooks_refuse_changed_attested_identity(self):
        path = self.state / 'provider-attestation.json'
        record = json.loads(path.read_bytes())
        record['providerManifest']['embeddingFingerprint'] = '0' * 64
        path.write_bytes(encoded(record))
        for action in ('recall', 'gate'):
            with self.subTest(action=action):
                status, payload, err = self.run_hook(action)
                self.assertEqual((status, payload), (3, None))
                self.assertIn('worker_attestation_changed', err)
                self.assertEqual(self.install.events()[-1]['outcome'], 'refused')
