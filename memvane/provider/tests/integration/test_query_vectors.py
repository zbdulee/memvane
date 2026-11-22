"""The provider file paths recorded in sealed records and the in-process query round trip."""
import os
import subprocess
import threading
import unittest
from unittest import mock

from memvane.kernel import bundle_paths
from memvane.kernel.errors import IndexContractError
from memvane.provider.api import query_vectors


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


class Handle:
    """A provider handle whose query round trip is ``answer(text, timeout)``."""

    manifest = {'approvedProviderId': 'synthetic'}

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def _embed(self, kind, text, title=None, path=None, *, timeout=None):
        self.asked.append((kind, len(text), threading.current_thread().name))
        return self.answer(text, timeout)


class QueryVectors(unittest.TestCase):
    ALLOWED = frozenset(('provider_unavailable', 'provider_closure_unapproved'))

    def setUp(self):
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('MEMVANE_TEST_RUNTIME_ROOT', None)  # The production path: no fixture answers.

    def ask(self, handle, prompt='q', budget=1.0, chunks=True):
        return query_vectors(handle, prompt, budget, chunks=chunks, allowed=self.ALLOWED)

    def refusal(self, *args, **kwargs):
        with self.assertRaises(IndexContractError) as caught:
            self.ask(*args, **kwargs)
        return str(caught.exception)

    def test_the_request_is_the_handles_round_trip_on_one_named_thread_and_no_process(self):
        handle = Handle(lambda text, timeout: [1.0, 0.0])
        with mock.patch.object(subprocess, 'Popen') as popen:
            self.assertEqual(self.ask(handle), ([[1.0, 0.0]], 1))
            self.assertEqual(self.ask(handle, chunks=False), ([[1.0, 0.0]], 1))
        self.assertEqual(handle.asked, [('query', 1, 'memvane-query-embed')] * 2)
        self.assertEqual(popen.call_count, 0)

    def test_the_time_given_to_a_request_is_what_the_recall_has_left_and_never_more_than_the_adapter_allows(self):
        seen = []
        handle = Handle(lambda text, timeout: seen.append(timeout) or [1.0, 0.0])
        self.ask(handle, budget=0.5)
        self.ask(handle, budget=30)
        self.ask(handle, budget=0.5, chunks=False)
        self.assertTrue(0 < seen[0] <= 0.5 and seen[1] == 2.0 and 0 < seen[2] <= 0.5, seen)
        # No time left: nothing is asked.
        self.assertEqual(self.refusal(handle, budget=0), 'provider_unavailable')
        self.assertEqual(self.refusal(handle, budget=0, chunks=False), 'provider_unavailable')
        self.assertEqual(len(seen), 3)

    def test_a_request_that_outlasts_the_recall_is_given_up_and_the_recall_goes_on(self):
        release = threading.Event()
        self.addCleanup(release.set)
        handle = Handle(lambda text, timeout: release.wait(30) and [1.0, 0.0])
        self.assertEqual(self.refusal(handle, budget=0.05), 'provider_unavailable')
        self.assertEqual(len(handle.asked), 1)

    def test_provider_errors_outside_allowed_become_result_invalid(self):
        def refuse(code):
            def answer(text, timeout):
                raise IndexContractError(code)
            return Handle(answer)
        self.assertEqual(self.refusal(refuse('provider_closure_unapproved')), 'provider_closure_unapproved')
        self.assertEqual(self.refusal(refuse('provider_recipe_mismatch')), 'provider_result_invalid')
        self.assertEqual(self.refusal(refuse('provider_closure_unapproved'), chunks=False),
                         'provider_closure_unapproved')

    def test_an_unexpected_failure_of_the_round_trip_reaches_the_caller_as_it_is(self):
        def broken(text, timeout):
            raise RuntimeError('adapter')
        with self.assertRaises(RuntimeError):
            self.ask(Handle(broken))

    def test_windows_that_fail_leave_the_vectors_that_answered(self):
        def answer(text, timeout):
            if len(text) > 1200:
                raise IndexContractError('provider_result_invalid')
            return [float(len(text)), 1.0]
        handle = Handle(answer)
        vectors, requested = self.ask(handle, prompt='x' * 5000)
        self.assertEqual((len(vectors), requested), (4, 5))
        self.assertEqual([size for _kind, size, _thread in handle.asked], [5000, 1200, 1200, 1200, 1200])


if __name__ == '__main__':
    unittest.main()
