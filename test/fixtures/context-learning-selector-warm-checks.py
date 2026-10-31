"""Recall latency: selector warm-up after heal, cold-service trigger, exact vector math."""
import hashlib
import json
import math
from pathlib import Path
import random
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from context_learning import inference_service, selector_heal
from context_learning.contracts import digest
from context_learning.store import canonical


def private(path, value):
    path.write_text(json.dumps(value)); path.chmod(0o600)


class WarmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qmd-warm-')
        self.state = Path(self.temp.name).resolve(); self.state.chmod(0o700)
        artifact = self.state / 'head.artifact'; artifact.write_text('head'); artifact.chmod(0o600)
        self.active = {'schema_version': 1, 'artifact_path': str(artifact),
                       'artifact_sha256': hashlib.sha256(b'head').hexdigest()}
        private(self.state / 'active-checkpoint.json', self.active)

    def tearDown(self):
        self.temp.cleanup()

    def test_warm_sends_one_request_the_worker_accepts(self):
        sent = []
        policy = {'trainerArgv': [sys.executable, 'adapter', 'model'], 'timeoutSeconds': 30, 'maxRssMib': 4096}
        def predict(state, policy_, request, sha, *, deadline, **kw):
            sent.append((state, policy_, request, sha, deadline - time.monotonic()))
            return {}, {}
        with patch.object(inference_service, '_policy', return_value=(policy, policy)), \
                patch.object(inference_service, 'predict', side_effect=predict):
            self.assertEqual(selector_heal.warm('/p', state=self.state), {'status': 'selector_warm'})
        state, used, request, sha, left = sent[0]
        self.assertEqual((state, used, sha), (self.state, policy, self.active['artifact_sha256']))
        self.assertTrue(0 < left <= selector_heal.WARM_SECONDS)
        # The exact shape the hosted worker validates (_validate_request).
        self.assertEqual(set(request), {'schema_version', 'artifact_path', 'cases'})
        self.assertEqual(request['artifact_path'], self.active['artifact_path'])
        (case,) = request['cases']
        self.assertEqual(set(case), {'request_id', 'prompt', 'candidates', 'input_sha256'})
        self.assertEqual(case['input_sha256'],
                         digest(canonical({k: v for k, v in case.items() if k != 'input_sha256'})))
        (row,) = case['candidates']
        self.assertEqual(set(row), {'id', 'revision_sha256', 'input_text', 'input_kind'})
        self.assertEqual(len(row['revision_sha256']), 64)

    def test_upkeep_warms_only_after_a_ready_or_prepared_heal(self):
        import laya_bundle_upgrade as upgrade
        from context_learning import state_move
        private(self.state / selector_heal.POLICY, {})
        events, warmed = [], []
        def event(state, name, outcome, reason=None, **fields):
            events.append((name, outcome, reason))
        for healed, expected in (({'status': 'selector_ready'}, ['/p']),
                                 ({'status': 'selector_prepared', 'rebound': False}, ['/p']),
                                 (selector_heal.HealDeferred('learning_prepare_busy'), [])):
            events.clear(); warmed.clear()
            heal = (patch.object(selector_heal, 'heal', side_effect=healed)
                    if isinstance(healed, Exception) else
                    patch.object(selector_heal, 'heal', return_value=healed))
            with patch.object(state_move, 'move', return_value={'status': 'learning_state_current'}), \
                    patch.object(selector_heal, 'namespace', return_value=self.state), heal, \
                    patch.object(selector_heal, 'warm',
                                 side_effect=lambda project, **kw: warmed.append(project) or {'status': 'selector_warm'}), \
                    patch.object(upgrade, '_learning_event', side_effect=event):
                out = upgrade._learning_upkeep('/p', self.state.parent, None)
            self.assertEqual(warmed, expected, healed)
            if expected:
                self.assertEqual(out['selectorWarm'], 'selector_warm')
                self.assertIn(('learning.warm', 'selector_warm', None), events)

    def test_a_cold_service_requests_the_worker(self):
        import laya_bundle_upgrade, laya_host_response
        requested = []
        decision = {'projectRoot': '/p', 'journalPath': '/j'}
        with patch.object(laya_bundle_upgrade, 'request',
                          side_effect=lambda *a: requested.append(a)):
            for reason in ('resident_service_deadline_expired', 'resident_service_starting',
                           'inference_attestation_required', 'invalid_pool', 'timeout'):
                laya_host_response._request_selector_heal(decision, reason)
        self.assertEqual([a[0] for a in requested], ['/p'] * 3)


class ExactVectorMathTests(unittest.TestCase):
    def test_sums_equal_the_generator_forms_bit_for_bit(self):
        import laya_index_vector as vector
        rng = random.Random(7)
        for dimension in (3, 384, 768):
            for _ in range(20):
                raw = struct.pack('<%df' % dimension, *(rng.uniform(-2, 2) for _ in range(dimension)))
                values = vector._unpack(raw, dimension)
                other = struct.unpack('<%df' % dimension, struct.pack(
                    '<%df' % dimension, *(rng.uniform(-2, 2) for _ in range(dimension))))
                self.assertEqual(vector._sum_squares(values), math.fsum(v * v for v in values))
                self.assertEqual(vector._dot(values, other),
                                 math.fsum(a * b for a, b in zip(values, other)))

    def test_non_finite_or_zero_blobs_are_still_refused(self):
        import laya_index_vector as vector
        from laya_index_a import IndexContractError
        for bad in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(IndexContractError):
                vector._unpack(struct.pack('<3f', 1.0, bad, 0.5), 3)
        with self.assertRaises(IndexContractError):
            vector._unpack(struct.pack('<3f', 0.0, 0.0, 0.0), 3)
        with self.assertRaises(IndexContractError):
            vector._blob([1.0, float('nan'), 0.5], 3)
        self.assertEqual(vector._unpack(struct.pack('<2f', 3.0, 4.0), 2), (3.0, 4.0))


if __name__ == '__main__':
    unittest.main()
