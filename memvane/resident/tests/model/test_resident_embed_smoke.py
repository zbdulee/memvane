"""Verify the model smoke's isolated request path without loading a model."""
import contextlib
import io
from pathlib import Path
import runpy
import unittest
from unittest import mock

from memvane.provider import api as provider_api
from memvane.resident import resident_service


class ModelSmokeFixture(unittest.TestCase):
    def test_smoke_sends_one_document_and_query_and_stops_worker(self):
        selected = {key:'a'*64 for key in ('nodeSha256','moduleEntrySha256','modelSha256')}
        selected.update(nodePath='/synthetic/node', moduleEntryPath='/synthetic/module',
                        modelPath='/synthetic/model', runtimeId='fixture')
        profile = {'profile_id':'p', 'model_id':'m', 'embedding_fingerprint':'f',
                   'dimension':768, 'metric':'cosine'}
        adapter = mock.Mock(profile=profile)
        adapter.embed_document_chunks.return_value = [{'vector':[0.0]*768}]
        adapter.side_effect = lambda *_: {'vector':[0.0]*768}
        service = mock.Mock()
        script = Path(__file__).with_name('resident_embed_smoke.py')
        with mock.patch.dict('os.environ', {}, clear=False), \
             mock.patch.object(provider_api, 'select', return_value=selected), \
             mock.patch.object(provider_api, 'hash_file', return_value='a'*64), \
             mock.patch.object(provider_api, 'recipe_fingerprint', return_value='b'*64), \
             mock.patch.object(provider_api, 'reviewed_provider', return_value=adapter), \
             mock.patch.object(resident_service, 'default_start_worker', return_value=service) as start, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            runpy.run_path(str(script), run_name='__main__')
        self.assertIn('"status": "ready"', output.getvalue())
        adapter.embed_document_chunks.assert_called_once()
        self.assertEqual(adapter.call_count, 1)
        start.assert_called_once()
        service.stop.assert_called_once_with()
