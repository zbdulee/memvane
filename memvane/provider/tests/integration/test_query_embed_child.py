"""The provider file paths recorded in sealed records and the query embed child protocol."""
import json
import subprocess
import unittest

from memvane.kernel import bundle_paths
from memvane.provider.api import query_embed_child_argv


class ProviderPaths(unittest.TestCase):
    def test_recorded_relative_paths_are_fixed(self):
        # Attestation providerRelativePath and artifact sha maps hold these exact strings.
        self.assertEqual(bundle_paths.PROVIDER_ADAPTERS_PREFIX, 'memvane/provider/adapters/')
        self.assertEqual(bundle_paths.PROVIDER_RELATIVE_PATH,
                         'memvane/provider/adapters/local_embed_provider.py')
        self.assertEqual(bundle_paths.WORKER_RELATIVE_PATH,
                         'memvane/provider/adapters/laya_local_embed_worker.mjs')
        self.assertEqual(bundle_paths.CHUNKER_RELATIVE_PATH,
                         'memvane/provider/adapters/laya_qmd_chunking.mjs')
        for relative in (bundle_paths.PROVIDER_RELATIVE_PATH, bundle_paths.WORKER_RELATIVE_PATH,
                         bundle_paths.CHUNKER_RELATIVE_PATH):
            self.assertTrue(bundle_paths.bundle_file(relative).is_file(), relative)

    def test_prefix_check_requires_exact_str(self):
        class Text(str):
            pass
        self.assertTrue(bundle_paths.is_provider_relative_path('memvane/provider/adapters/x.py'))
        for value in ('memvane/provider/x.py', '', None, 1, b'memvane/provider/adapters/x.py',
                      Text('memvane/provider/adapters/x.py')):
            self.assertFalse(bundle_paths.is_provider_relative_path(value), value)


class QueryEmbedChild(unittest.TestCase):
    def run_child(self, request):
        return subprocess.run(query_embed_child_argv(), input=json.dumps(request), text=True,
                              capture_output=True, timeout=60)

    def test_argv_is_isolated_and_rooted_at_the_bundle(self):
        argv = query_embed_child_argv()
        self.assertEqual(argv[1:4], ['-B', '-I', '-c'])
        self.assertEqual(argv[5], str(bundle_paths.plugin_root()))
        self.assertEqual(len(argv), 6)

    def test_provider_errors_outside_allowed_become_result_invalid(self):
        request = {'manifest': {}, 'prompt': 'q', 'timeout': 1}
        allowed = self.run_child({**request, 'allowed': ['provider_closure_unapproved']})
        self.assertEqual((allowed.returncode, json.loads(allowed.stdout)),
                         (0, {'error': 'provider_closure_unapproved'}))
        hidden = self.run_child({**request, 'allowed': []})
        self.assertEqual((hidden.returncode, json.loads(hidden.stdout)),
                         (0, {'error': 'provider_result_invalid'}))

    def test_malformed_request_fails_the_process(self):
        result = self.run_child({})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
