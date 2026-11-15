"""The two product checks the fixture seam reaches: the reviewed-provider check and the query child.

Both turn on only through ``fixture_port.active``: an absolute
``MEMVANE_TEST_RUNTIME_ROOT`` holding an owner-only regular spec file. In
every other case the fixture handle is not a reviewed provider and the query
child answers exactly as the real provider path does (the fixture manifest is
not a reviewed manifest: ``provider_closure_unapproved``).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from memvane.provider import api as provider
from memvane.provider import fixture_port
from memvane.provider.types import FIXTURE_FILE

SPEC = {'schema': fixture_port.SPEC_SCHEMA, 'dimension': 3}
REAL_ANSWER = '{"error": "provider_closure_unapproved"}\n'


class FixtureSeamChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.chmod(self.root, 0o700)
        # Any manifest the fixture handle carries; the real provider refuses it unreviewed.
        self.manifest = {'approvedProviderId': fixture_port.PROVIDER_ID, 'modelId': fixture_port.MODEL,
                         'modelSha256': fixture_port.MODEL_SHA, 'dimension': 3}
        self.handle = fixture_port.FixtureProvider(SPEC).reviewed_provider(self.manifest)

    def write_spec(self, spec=None, mode=0o600, name=FIXTURE_FILE):
        path = self.root / name
        path.write_text(json.dumps(dict(SPEC, **(spec or {}))))
        os.chmod(path, mode)
        return path

    def environ(self, value):
        environ = {key: item for key, item in os.environ.items() if key != 'MEMVANE_TEST_RUNTIME_ROOT'}
        if value is not None:
            environ['MEMVANE_TEST_RUNTIME_ROOT'] = value
        return environ

    def child(self, value, allowed=('provider_closure_unapproved', 'provider_unavailable')):
        request = {'manifest': self.manifest, 'prompt': 'q', 'timeout': 1, 'allowed': list(allowed)}
        result = subprocess.run(provider.query_embed_child_argv(), input=json.dumps(request), text=True,
                                capture_output=True, timeout=60, env=self.environ(value))
        return result.returncode, result.stdout

    def off_cases(self):
        """``(label, env value)`` after arranging the case's spec file; each leaves the seam off."""
        yield 'no env', None
        self.write_spec()
        yield 'relative root', os.path.relpath(self.root)
        os.unlink(self.root / FIXTURE_FILE)
        # The seam has no stat stand-in for a foreign owner: a group/other-readable
        # spec breaks the same owner-only rule.
        self.write_spec(mode=0o644)
        yield 'not owner-only', str(self.root)
        os.unlink(self.root / FIXTURE_FILE)
        target = self.write_spec(name='elsewhere.json')
        os.symlink(target, self.root / FIXTURE_FILE)
        yield 'symlink spec', str(self.root)

    def test_without_an_active_seam_both_checks_stay_production(self):
        seen = []
        for label, value in self.off_cases():
            with self.subTest(case=label):
                with mock.patch.dict(os.environ, self.environ(value), clear=True):
                    self.assertIsNone(fixture_port.active())
                    self.assertFalse(provider.is_reviewed_provider(self.handle))
                answer = self.child(value)
                self.assertEqual(answer, (0, REAL_ANSWER))
                seen.append(label)
        self.assertEqual(seen, ['no env', 'relative root', 'not owner-only', 'symlink spec'])

    def test_an_active_seam_accepts_only_its_handle_and_answers_from_the_spec(self):
        self.write_spec()
        with mock.patch.dict(os.environ, self.environ(str(self.root)), clear=True):
            self.assertTrue(provider.is_reviewed_provider(self.handle))
            self.assertTrue(provider.is_reviewed_provider(provider.reviewed_provider(self.manifest)))

            class Subclass(type(self.handle)):
                pass
            for value in (None, object(), self.manifest, Subclass(self.manifest, {}, [1.0, 0.0, 0.0])):
                self.assertFalse(provider.is_reviewed_provider(value), value)
        self.assertEqual(self.child(str(self.root)), (0, '{"vector":[1.0,0.0,0.0]}\n'))
        self.write_spec({'queryEmbedding': 'provider_unavailable'})
        self.assertEqual(self.child(str(self.root)), (0, '{"error":"provider_unavailable"}\n'))
        self.assertEqual(self.child(str(self.root), allowed=()),
                         (0, '{"error":"provider_result_invalid"}\n'))

    def test_the_spec_accepts_only_the_two_query_answers(self):
        self.assertEqual(fixture_port.FixtureProvider(SPEC).query_embedding, 'vector')
        for value in ('provider_identity_mismatch', '', None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fixture_port.FixtureProvider(dict(SPEC, queryEmbedding=value))


if __name__ == '__main__':
    unittest.main()
