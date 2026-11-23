"""The exact semantic search gets ``min(2.0, time the recall has left)``, for prompt and edit recalls alike.

``RecallInstallCase`` with an admitted fixture provider, so every recall has a
query vector. The store's ``search_exact`` is observed through
``store.api.open_store``; the assertions are its call arguments.
"""
from __future__ import annotations

import time
from unittest import mock

from memvane.journal import api as journal_api
from memvane.recall import reader_dispatch, recall_scope, selected_recall
from memvane.recall.tests.support import RecallInstallCase
from memvane.recall.types import RecallTrace
from memvane.store import api as store_api
from tests.support.install_builder import ROOT

PROMPT = 'what marker does the heron protocol keep?'


class _MemoryTrace(RecallTrace):
    def __init__(self):
        self.data = {}

    def fields(self, **fields):
        self.data.update(fields)


class SemanticDeadline(RecallInstallCase):
    CARDS = 2

    def setUp(self):
        super().setUp()
        self.install.provider.admit_queries()
        self.calls = []
        real_open = store_api.open_store

        def open_store(*args, **kwargs):
            store = real_open(*args, **kwargs)
            real_exact = store.search_exact

            def search_exact(*call, **options):
                self.calls.append(options['deadline_seconds'])
                return real_exact(*call, **options)
            store.search_exact = search_exact
            return store
        patch = mock.patch.object(store_api, 'open_store', side_effect=open_store)
        patch.start()
        self.addCleanup(patch.stop)

    def edit_recall(self, seconds):
        decision = journal_api.select_route(str(self.project), action='recall')
        with journal_api.resolve_endpoint(self.project, action='recall', host_root=ROOT) as endpoint:
            _record, _attestation, proof = journal_api.recheck_reader(endpoint)
        trace = _MemoryTrace()
        with recall_scope.recall_scope():
            recall_scope.set_edit_recall(deadline=time.monotonic() + seconds,
                                         selector_deadline=time.monotonic() + seconds)
            answer = selected_recall.serve_endpoint_recall(
                'claude', {'cwd': str(self.project), 'prompt': PROMPT, 'session_id': 'edit-deadline'},
                cwd=str(self.project), trace=trace, route_decision=decision, main_proof=proof)
        return answer, trace.data

    def test_the_constant_is_two_seconds(self):
        self.assertEqual(reader_dispatch.SEMANTIC_DEADLINE_SECONDS, 2.0)

    def test_a_prompt_recall_gives_the_semantic_search_two_seconds(self):
        context, _err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual(self.calls, [2.0])
        self.assertEqual(event['semantic'], 'complete')

    def test_an_edit_recall_gives_the_semantic_search_two_seconds(self):
        answer, data = self.edit_recall(10)
        self.assertTrue(answer.rows)
        self.assertEqual(self.calls, [2.0])
        self.assertEqual(data['semantic'], 'complete')

    def test_a_recall_with_less_time_left_gives_what_is_left_after_the_reserve(self):
        # One second of the remaining time stays for locations, selector and rendering.
        answer, _data = self.edit_recall(2.5)
        self.assertTrue(answer.rows)
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(self.calls[0], 0)
        self.assertLess(self.calls[0], 1.5)
