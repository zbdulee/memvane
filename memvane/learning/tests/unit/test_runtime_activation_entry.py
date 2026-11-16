import unittest
from unittest import mock

from memvane.learning import runtime_activation as activation


class RuntimeActivationTests(unittest.TestCase):
    def test_activation_only_records_choice_even_under_pressure(self):
        with mock.patch.object(activation, '_memory_admission', return_value=False) as admit, \
             mock.patch.object(activation, '_stage_pending', return_value={'status':'activated_pending_proof'}) as stage:
            result = activation.activate('/tmp/venv/bin/python', '/tmp/model')
        self.assertEqual(result['status'], 'activated_pending_proof')
        stage.assert_called_once_with('/tmp/venv/bin/python', '/tmp/model')
        admit.assert_not_called()

    def test_first_use_proves_once_under_lease(self):
        pending = {'executable':'/tmp/venv/bin/python','model_dir':'/tmp/model'}
        with mock.patch.object(activation, '_pending', side_effect=[pending, pending, None]), \
             mock.patch.object(activation, '_memory_admission', return_value=True), \
             mock.patch('memvane.learning.context_learning.selector_heal._prepare_lease', return_value=9), \
             mock.patch.object(activation.os, 'close') as close, \
             mock.patch.object(activation, '_activate_pending', return_value={'status':'activated'}) as prove:
            self.assertEqual(activation.ensure_pending()['status'], 'activated')
            self.assertEqual(activation.ensure_pending()['status'], 'no_pending')
        prove.assert_called_once_with(pending)
        close.assert_called_once_with(9)

    def test_resource_deferral_does_not_consume_pending(self):
        with mock.patch.object(activation, '_pending', return_value={'executable':'/tmp/x'}), \
             mock.patch.object(activation, '_memory_admission', return_value=False), \
             mock.patch.object(activation, '_activate_pending') as prove:
            with self.assertRaises(activation.ResourceDeferred):
                activation.ensure_pending()
        prove.assert_not_called()

    def test_failed_proof_rolls_back_and_records_notice(self):
        pending = {'executable':'/tmp/venv/bin/python','model_dir':'/tmp/model'}
        with mock.patch.object(activation, '_pending', return_value=pending), \
             mock.patch.object(activation, '_memory_admission', return_value=True), \
             mock.patch('memvane.learning.context_learning.selector_heal._prepare_lease', return_value=9), \
             mock.patch.object(activation.os, 'close'), \
             mock.patch.object(activation, '_activate_pending', side_effect=ValueError('proof_failed')), \
             mock.patch.object(activation, '_mark_failed') as fail:
            with self.assertRaisesRegex(ValueError, 'proof_failed'):
                activation.ensure_pending()
        fail.assert_called_once_with(pending, 'proof_failed')

    def test_staging_writes_private_pending_and_leaves_active_unchanged(self):
        import json
        import tempfile
        from pathlib import Path
        from memvane.learning.context_learning.adapter import MODEL_FILES
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); root.chmod(0o700)
            generation = root/'generations'/'g-fixture'; generation.mkdir(parents=True, mode=0o700)
            (root/'generations').chmod(0o700)
            executable = generation/'venv'/'bin'/'python'; executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nexit 0\n'); executable.chmod(0o700)
            prepared = {'schema_version':1, 'executable':str(executable),
                        'runtime_identity_sha256':'a'*64, 'uv_sha256':'b'*64, 'lock_sha256':'c'*64}
            (generation/'prepared.json').write_text(json.dumps(prepared)); (generation/'prepared.json').chmod(0o600)
            model = root/'model'; model.mkdir()
            for name in MODEL_FILES:
                path = model/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
            active = root/'active.json'; active.write_text('old active remains'); active.chmod(0o600)
            with mock.patch.object(activation, '_root', return_value=root), \
                 mock.patch.object(activation, '_memory_admission') as admission:
                result = activation.activate(str(executable), str(model))
                pending = activation._pending()
            self.assertEqual(result['status'], 'activated_pending_proof')
            self.assertEqual(active.read_text(), 'old active remains')
            self.assertEqual(pending['executable'], str(executable))
            self.assertEqual(len(pending['model_identity']), 64)
            self.assertEqual((root/activation.PENDING).stat().st_mode & 0o777, 0o600)
            admission.assert_not_called()

    def test_failed_pending_is_removed_and_previous_pointer_retained(self):
        import tempfile
        from pathlib import Path
        from memvane.learning.context_learning.runtime_installer import _write_pointer
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); root.chmod(0o700)
            row = {'schema':'memvane-learning-pending-v1','executable':'/tmp/python',
                   'model_dir':'/tmp/model','model_identity':'a'*64,'runtime_identity_sha256':'b'*64}
            _write_pointer(root/activation.PENDING, row)
            active = root/'active.json'; active.write_text('old'); active.chmod(0o600)
            with mock.patch.object(activation, '_root', return_value=root):
                activation._mark_failed(row, 'synthetic_failure')
                self.assertIsNone(activation._pending())
                self.assertTrue(activation.failure_notice())
            self.assertEqual(active.read_text(), 'old')

    def test_first_proof_is_requested_only_when_cycle_is_due(self):
        from memvane.learning.context_learning import schedule
        queue = {'splits': {'train': [], 'validation': [], 'evaluation': []}}
        previous = {'last_run_at': 100, 'next_due': 100000, 'interval_hours': 24,
                    'family_hashes': [], 'revisions_sha256': schedule.digest(schedule.canonical([]))}
        proof = mock.Mock()
        with mock.patch.object(schedule, 'build_queue', return_value=queue), \
             mock.patch.object(schedule, '_load_state', return_value=previous), \
             mock.patch.object(schedule, 'run_cycle') as train:
            result = schedule._run_due_cycle_locked('/tmp', 'cycle', 'v', lambda:{}, None,
                incumbent_artifact='/tmp/artifact', now=101, before_run=proof)
        self.assertEqual(result['status'], 'not_due')
        proof.assert_not_called(); train.assert_not_called()
        with mock.patch.object(schedule, 'build_queue', return_value=queue), \
             mock.patch.object(schedule, '_load_state', return_value=None), \
             mock.patch.object(schedule, 'run_cycle') as train:
            proof.side_effect = activation.ResourceDeferred('learning_prepare_memory_pressure')
            result = schedule._run_due_cycle_locked('/tmp', 'cycle', 'v', lambda:{}, None,
                incumbent_artifact='/tmp/artifact', now=101, before_run=proof)
        self.assertEqual(result['status'], 'deferred_resource')
        proof.assert_called_once(); train.assert_not_called()
