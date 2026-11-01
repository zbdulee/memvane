"""Cause codes for a refused member embedding; no model, no live data."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
from laya_index_a import IndexContractError
import laya_vector_diagnose as vd

MANIFEST = {'approvedProviderId': 'p', 'providerArtifactSha256': 'a' * 64,
            'embeddingFingerprint': 'f' * 64, 'dimension': 2}
IDENTITY = {key: MANIFEST[key] for key in vd.IDENTITY}


def answer(chunks):
    return {**IDENTITY, 'ok': True, 'result': {'chunks': chunks}}


GOOD = {'seq': 0, 'pos': 0, 'vector': [0.5, 0.5]}


class RuleTests(unittest.TestCase):
    def test_rules_mirror_the_provider_in_order(self):
        cases = [
            ('text', 'envelope_invalid'),
            ({**IDENTITY, 'approvedProviderId': 'other', 'ok': True, 'result': {}},
             'identity_mismatch'),
            ({**IDENTITY, 'ok': False, 'result': None}, 'worker_failure'),
            ({**IDENTITY, 'ok': False, 'result': None, 'refused': 'x'}, 'worker_refused'),
            ({'ok': True, 'result': {'chunks': [GOOD]}}, 'envelope_invalid'),
            ({**answer([GOOD]), 'extra': 1}, 'envelope_invalid'),
            ({**IDENTITY, 'ok': True, 'result': {'chunks': [GOOD], 'more': 1}}, 'result_invalid'),
            (answer([]), 'chunk_count_invalid'),
            (answer([GOOD] * 1025), 'chunk_count_invalid'),
            (answer([{'seq': 0, 'pos': 0}]), 'chunk_shape_invalid'),
            (answer([{**GOOD, 'seq': 1}]), 'chunk_seq_invalid'),
            (answer([{**GOOD, 'pos': '0'}]), 'chunk_shape_invalid'),
            (answer([GOOD, {'seq': 1, 'pos': 0, 'vector': [0.1, 0.2]}]),
             'chunk_pos_not_increasing'),
            (answer([{**GOOD, 'vector': [0.1]}]), 'dimension_mismatch'),
            (answer([{**GOOD, 'vector': [0.1, True]}]), 'vector_value_invalid'),
            (answer([{**GOOD, 'vector': [0.1, float('inf')]}]), 'non_finite_vector'),
            (answer([{**GOOD, 'vector': [1e39, 0.0]}]), 'vector_out_of_float32_range'),
            (answer([{**GOOD, 'vector': [0.0, 0.0]}]), 'zero_norm_vector'),
            (answer([{**GOOD, 'vector': [1e-46, 0.0]}]), 'zero_norm_vector'),
            (answer([GOOD, {'seq': 1, 'pos': 5, 'vector': [1.0, 0.0]}]), 'unknown'),
        ]
        for envelope, code in cases:
            self.assertEqual(vd.result_rule_code(envelope, MANIFEST), code, str(envelope)[:80])

    def test_rule_codes_agree_with_the_provider_vector_check(self):
        from laya_index_vector import _blob
        for vector in ([0.0, 0.0], [1e39, 0.0], [float('nan'), 1.0]):
            with self.assertRaises(IndexContractError):
                _blob(vector, 2)
            self.assertNotEqual(vd.result_rule_code(answer([{**GOOD, 'vector': vector}]),
                                                    MANIFEST), 'unknown')


class ChildTests(unittest.TestCase):
    def fake(self, reply):
        module = types.ModuleType('fake_provider_for_diagnosis')
        module.json = json

        class Provider:
            def embed_document_chunks(self, body, path, *, timeout):
                envelope = module.json.loads(reply)
                code = vd.result_rule_code(envelope, MANIFEST)
                if code != 'unknown':
                    raise IndexContractError('provider_result_invalid')
                return envelope['result']['chunks']
        return module, Provider()

    def test_child_names_the_rule_and_restores_its_module(self):
        bad = json.dumps(answer([GOOD, {'seq': 1, 'pos': 0, 'vector': [1.0, 0.0]}]))
        module, provider = self.fake(bad)
        self.assertEqual(vd._observe_and_embed(module, provider, 'b', 'a.md', 120, MANIFEST),
                         'chunk_pos_not_increasing')
        self.assertIs(module.json, json)
        module, provider = self.fake(json.dumps(answer([GOOD])))
        self.assertEqual(vd._observe_and_embed(module, provider, 'b', 'a.md', 120, MANIFEST),
                         'not_reproduced')

    def test_child_reports_an_empty_answer(self):
        module = types.ModuleType('fake_provider_for_diagnosis')
        module.json = json

        class Silent:
            def embed_document_chunks(self, body, path, *, timeout):
                try:
                    module.json.loads('')
                except ValueError as exc:
                    raise IndexContractError('provider_result_invalid') from exc
        self.assertEqual(vd._observe_and_embed(module, Silent(), 'b', 'a.md', 120, MANIFEST),
                         'empty_answer')


class ProcessTests(unittest.TestCase):
    def test_diagnosis_runs_in_a_separate_bytecode_free_process(self):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, b'{"cause":"zero_norm_vector"}', b'')
        self.assertEqual(vd.diagnose(MANIFEST, 'body', 'w/a.md', timeout=120, runner=runner),
                         'zero_norm_vector')
        (argv, kw), = calls
        self.assertEqual(argv[1:], ['-B', str(CORE / 'laya_vector_diagnose.py')])
        self.assertEqual(kw['env']['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual(kw['timeout'], 120 + vd.CHILD_MARGIN_SECONDS)
        self.assertEqual(json.loads(kw['input'])['path'], 'w/a.md')

    def test_bad_child_output_or_failure_is_unknown(self):
        for out, rc in ((b'{"cause":"has space"}', 0), (b'nope', 0), (b'{"cause":"x"}', 1)):
            runner = lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, b'')
            self.assertEqual(vd.diagnose(MANIFEST, 'b', 'a.md', timeout=1, runner=runner),
                             'unknown')

        def timeout(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw['timeout'])
        self.assertEqual(vd.diagnose(MANIFEST, 'b', 'a.md', timeout=1, runner=timeout), 'unknown')

    def test_real_child_with_an_unusable_manifest_answers_unknown_without_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary:
            before = set((CORE / '__pycache__').glob('*')) if (CORE / '__pycache__').exists() else set()
            self.assertEqual(vd.diagnose({'bad': True}, 'b', 'a.md', timeout=5), 'unknown')
            after = set((CORE / '__pycache__').glob('*')) if (CORE / '__pycache__').exists() else set()
            self.assertEqual(after - before, set())
            self.assertTrue(Path(temporary).is_dir())


if __name__ == '__main__':
    unittest.main()
