"""Sync worker rules that need no install: budgets, codes, route sources, the view chunker."""
import unittest

from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.store.api import qmd_first_pass
from memvane.store.types import FALLBACK_CHARS
from memvane.sync import product_sync_worker as worker


class ExternalHintAdmission(unittest.TestCase):
    def test_incomplete_sync_never_drains_external_hints(self):
        from unittest.mock import patch
        from memvane.wiki.intake import api as intake
        with patch.object(intake, 'drain') as drain:
            for state in ('deferred', 'pending', 'blocked'):
                with self.subTest(state=state):
                    result = {'state': state, 'reason': 'synthetic'}
                    self.assertEqual(worker._after_sync({}, {}, None, result), result)
        drain.assert_not_called()


class Provider:
    """A provider handle answering ``positions(body)`` chunk positions; records every call."""

    def __init__(self, positions=None, refuse=False):
        self.positions, self.refuse, self.calls = positions, refuse, []

    def embed_document_chunks(self, body, path, **kwargs):
        self.calls.append(('chunks', path, kwargs))
        if self.refuse:
            raise IndexContractError('provider_member_refused')
        positions = self.positions(body) if self.positions else [pos for pos, _ in qmd_first_pass(body)]
        return [{'seq': seq, 'pos': pos, 'vector': [float(seq), 1.0]} for seq, pos in enumerate(positions)]

    def embed_document(self, body, title):
        self.calls.append(('document', title, body))
        if self.refuse:
            raise IndexContractError('provider_member_refused')
        return [9.0, 9.0]


def chunker_for(provider):
    context = {'stash': {}, 'acquire': lambda proof: provider}
    chunker, embedder = worker._schema3_chunker(context, lambda: {'proof': True})
    return context, chunker, embedder


def inputs(title, chunks):
    return ['title: ' + title + ' | text: ' + chunk['text'] for chunk in chunks]


class ViewChunker(unittest.TestCase):
    BODY = '# Kestrel\n' + ''.join('Line %03d of the kestrel perch notes.\n' % n for n in range(120))

    def test_chunker_receives_body_and_path_and_seals_title_in_embedding_input(self):
        seen = []
        class RecordingProvider(Provider):
            def embed_document_chunks(self, body, path, **kwargs):
                seen.append((body, path))
                return super().embed_document_chunks(body, path, **kwargs)
        provider = RecordingProvider()
        context, chunker, embedder = chunker_for(provider)
        document = {'body': 'plain card body', 'path': 'concepts/bird.md', 'title': 'Kestrel'}
        chunks = chunker(document, {})
        self.assertEqual(seen, [('plain card body', 'concepts/bird.md')])
        self.assertEqual([row['text'] for row in chunks], ['plain card body'])
        self.assertEqual(embedder(inputs('Kestrel', chunks), None), [[0.0, 1.0]])
        self.assertEqual(context['stash'], {})

    def test_provider_first_pass_positions_keep_the_vectors_of_the_one_call(self):
        provider = Provider()
        context, chunker, embedder = chunker_for(provider)
        chunks = chunker({'body': self.BODY, 'path': 'blobs/a.md', 'title': 'Kestrel'}, None)
        self.assertEqual([(c['position'], c['text']) for c in chunks], qmd_first_pass(self.BODY))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len(context['stash']), len(chunks))
        vectors = embedder(inputs('Kestrel', chunks), None)
        self.assertEqual(vectors, [[float(seq), 1.0] for seq in range(len(chunks))])
        # One chunked provider call; the embedder asked nothing more.
        self.assertEqual([call[0] for call in provider.calls], ['chunks'])
        self.assertEqual(context['stash'], {})

    def test_a_resplit_document_gets_the_fallback_windows_each_embedded_alone(self):
        # The provider re-split over its token window: one more chunk position than the first pass.
        provider = Provider(lambda body: [pos for pos, _ in qmd_first_pass(body)] + [len(body) - 1])
        context, chunker, embedder = chunker_for(provider)
        chunks = chunker({'body': self.BODY, 'path': 'blobs/a.md', 'title': 'Kestrel'}, None)
        fallback = qmd_first_pass(self.BODY, *FALLBACK_CHARS)
        self.assertEqual(FALLBACK_CHARS, (600, 90, 135))
        self.assertNotEqual([pos for pos, _ in fallback], [pos for pos, _ in qmd_first_pass(self.BODY)])
        self.assertEqual([(c['position'], c['text']) for c in chunks], fallback)
        self.assertEqual(context['stash'], {})
        vectors = embedder(inputs('Kestrel', chunks), None)
        self.assertEqual(vectors, [[9.0, 9.0]] * len(fallback))
        self.assertEqual([call[:2] for call in provider.calls[1:]], [('document', 'Kestrel')] * len(fallback))
        self.assertEqual([call[2] for call in provider.calls[1:]], [text for _pos, text in fallback])

    def test_a_refused_member_or_an_oversized_set_blocks_only_that_card(self):
        _context, chunker, embedder = chunker_for(Provider(refuse=True))
        for call in (lambda: chunker({'body': self.BODY, 'path': 'blobs/a.md', 'title': 'K'}, None),
                     lambda: embedder(['title: K | text: body'], None)):
            with self.assertRaises(worker._VectorMemberBlocked) as caught:
                call()
            self.assertEqual(str(caught.exception), 'provider_member_refused')
        # A re-split document whose fallback windows exceed the store's chunk limit.
        big = 'x' * (FALLBACK_CHARS[0] - FALLBACK_CHARS[1]) * 1100
        _context, chunker, _embedder = chunker_for(Provider(lambda body: [0, 1]))
        with self.assertRaises(worker._VectorMemberBlocked) as caught:
            chunker({'body': big, 'path': 'blobs/b.md', 'title': 'B'}, None)
        self.assertEqual(str(caught.exception), 'chunk_set_too_large')

    def test_an_embedding_input_the_worker_did_not_format_is_refused(self):
        _context, _chunker, embedder = chunker_for(Provider())
        for value in ('plain text', 'title:  | text: x', 'title: T | text: '):
            with self.assertRaises(worker._VectorMemberBlocked) as caught:
                embedder([value], None)
            self.assertEqual(str(caught.exception), 'embedding_input_unparsed')

    def test_other_provider_failures_propagate(self):
        class Failing(Provider):
            def embed_document_chunks(self, body, path, **kwargs):
                raise IndexContractError('provider_socket_closed')
        _context, chunker, _embedder = chunker_for(Failing())
        with self.assertRaisesRegex(IndexContractError, 'provider_socket_closed'):
            chunker({'body': self.BODY, 'path': 'blobs/a.md', 'title': 'K'}, None)


class Budgets(unittest.TestCase):
    def test_large_document_budget_counts_utf8_bytes(self):
        self.assertFalse(worker._large_initial_document('x' * 200_000))
        self.assertTrue(worker._large_initial_document('x' * 200_001))
        self.assertTrue(worker._large_initial_document('한' * 70_000))
        self.assertFalse(worker._large_initial_document(None))

    def test_only_a_large_document_gets_the_large_call_budget(self):
        provider = Provider()
        worker._embed_with_budget(provider, 'small body', 'blobs/s.md')
        worker._embed_with_budget(provider, 'x' * 200_001, 'blobs/l.md')
        self.assertEqual([(path, kwargs) for _kind, path, kwargs in provider.calls],
                         [('blobs/s.md', {}), ('blobs/l.md', {'timeout': 270})])


class Codes(unittest.TestCase):
    def test_a_deferral_keeps_its_admission_criterion(self):
        self.assertEqual(worker._deferred(ResourceDeferred('owner_busy')),
                         {'state': 'deferred', 'reason': 'owner_busy'})
        self.assertEqual(worker._deferred(ResourceDeferred('learning_deferred_resource',
                                                           {'ready': False, 'reason': 'memory_reserve'})),
                         {'state': 'deferred', 'reason': 'learning_deferred_resource', 'detail': 'memory_reserve'})

    def test_a_busy_kernel_gate_names_the_gate(self):
        for gate in ('provider', 'control', 'trainer', 'compute'):
            with self.subTest(gate=gate):
                self.assertEqual(worker._deferred(ResourceDeferred('learning_compute_busy', {'gate': gate})),
                                 {'state': 'deferred', 'reason': 'learning_compute_busy', 'gate': gate,
                                  'detail': 'gate_' + gate})
        # An admission criterion stays the detail; a gate that is not a fixed code is not recorded.
        self.assertEqual(worker._deferred(ResourceDeferred('learning_compute_busy',
                                                           {'reason': 'memory_reserve', 'gate': 'control'})),
                         {'state': 'deferred', 'reason': 'learning_compute_busy', 'gate': 'control',
                          'detail': 'memory_reserve'})
        self.assertEqual(worker._deferred(ResourceDeferred('learning_compute_busy', {'gate': '/tmp/x.lock'})),
                         {'state': 'deferred', 'reason': 'learning_compute_busy'})

    def test_only_a_fixed_code_survives(self):
        self.assertEqual(worker._code(IndexContractError('provider_start_failed')), 'provider_start_failed')
        self.assertEqual(worker._code(IndexContractError('private document path and content')), 'failed')
        self.assertEqual(worker._code(IndexContractError('x' * 61)), 'failed')

    def test_vector_detail_is_one_fixed_code_without_paths(self):
        detail = worker._schema3_vector_detail
        self.assertIsNone(detail({'vector': {'state': 'current'}}))
        self.assertIsNone(detail({'state': 'indexed'}))
        self.assertEqual(detail({'vector': {'state': 'published', 'publishMs': 12, 'providerMs': 3},
                                 'readerResettle': 'resealed'}), 'vec_published:pms3:pub12:res_resealed')
        self.assertEqual(detail({'vector': {'state': 'pending', 'reason': 'schema3_vector_budget',
                                            'remaining': 4, 'ordinal': 2}}), 'vec_pending_budget:rem4:ord2')
        self.assertRegex(detail({'vector': {'state': 'deferred', 'reason': 'Private Path/x.md'}}),
                         r'^vec_deferred_[a-z0-9_.:-]+$')
        self.assertLessEqual(len(detail({'vector': {'state': 'blocked', 'reason': 'r' * 200}})), 80)


class RouteSource(unittest.TestCase):
    def test_v1_source_root_is_passed_unchanged(self):
        self.assertEqual(worker._route_source({'sourceRoot': '/projects/a'}), '/projects/a')

    def test_malformed_sources_fail_with_fixed_codes(self):
        cases = {
            'selected_route_source_missing': {'projectRoot': '/p'},
            'selected_route_source_ambiguous': {'sourceRoot': '/p', 'collections': []},
            'selected_collection_roots_invalid': {'collections': [{'name': 'a'}], 'collectionRoots': {}},
            'selected_collection_roots_mismatch': {'collections': [{'name': 'a'}, {'name': 'b'}],
                                                   'collectionRoots': {'a': '/p'}},
        }
        for reason, decision in cases.items():
            with self.subTest(reason):
                with self.assertRaises(IndexContractError) as caught:
                    worker._route_source(decision)
                self.assertEqual(str(caught.exception), reason)


if __name__ == '__main__':
    unittest.main()


class OwnerOccupancy(unittest.TestCase):
    """``sync.profile``'s sync owner fields: every acquisition of the pass, measured around ``sync_owner``."""

    def acquisitions(self, *answers):
        from contextlib import contextmanager
        from unittest import mock
        calls, sink = iter(answers), []

        @contextmanager
        def owner(path, **kwargs):
            self.assertEqual(kwargs, {'wait': True})
            yield next(calls)
        token = worker._OWNER_PASS.set(sink)
        try:
            with mock.patch.object(worker, 'sync_owner', owner):
                for _answer in answers:
                    with worker._sync_owner('/synthetic/store.sqlite'):
                        pass
        finally:
            worker._OWNER_PASS.reset(token)
        return sink

    def test_each_acquisition_records_whether_it_got_the_owner(self):
        sink = self.acquisitions(object(), None, object())
        self.assertEqual([item['acquired'] for item in sink], [True, False, True])
        self.assertEqual(sink[1]['holdNs'], 0)
        fields = worker._owner_fields(sink)
        self.assertEqual((fields['ownerCalls'], fields['ownerBusy']), (3, 1))
        self.assertEqual(set(fields), {'ownerCalls', 'ownerBusy', 'ownerContended', 'ownerWaitMs',
                                       'ownerWaitMaxMs', 'ownerHoldMs', 'ownerHoldMaxMs'})

    def test_an_acquisition_that_waited_a_poll_interval_is_contended(self):
        step = worker.OWNER_CONTENDED_MS * 1000000
        sink = [{'acquired': True, 'waitNs': step, 'holdNs': 7 * step},
                {'acquired': True, 'waitNs': step - 1, 'holdNs': step},
                {'acquired': False, 'waitNs': 60 * step, 'holdNs': 0}]
        fields = worker._owner_fields(sink)
        self.assertEqual((fields['ownerContended'], fields['ownerBusy']), (1, 1))
        self.assertEqual((fields['ownerHoldMs'], fields['ownerHoldMaxMs']),
                         (8 * worker.OWNER_CONTENDED_MS, 7 * worker.OWNER_CONTENDED_MS))
        self.assertEqual(fields['ownerWaitMaxMs'], 60 * worker.OWNER_CONTENDED_MS)
        self.assertEqual(worker._owner_fields([]), {})

    def test_without_a_pass_nothing_is_recorded(self):
        from contextlib import contextmanager
        from unittest import mock

        @contextmanager
        def owner(path, **kwargs):
            yield object()
        with mock.patch.object(worker, 'sync_owner', owner):
            with worker._sync_owner('/synthetic/store.sqlite') as held:
                self.assertIsNotNone(held)
        self.assertIsNone(worker._OWNER_PASS.get())


class PassFields(unittest.TestCase):
    """``sync.profile``'s view of a settled pass: the main view apart from linked views (no paths)."""

    def test_linked_views_are_hashed_ids_with_their_states(self):
        result = {'state': 'indexed', 'reason': None,
                  'linkedWaiting': {'waiting': 1, 'held': 0, 'reasons': {'schema3_pass_budget': 1},
                                    'pending': 'linked_reoffer'},
                  'worktrees': [{'root': '/private/side', 'state': 'pending', 'reason': 'schema3_pass_budget'},
                                {'root': '/private/other', 'state': 'indexed', 'vector': {'state': 'pending'}}]}
        fields = worker._pass_fields(result, 'unchanged')
        self.assertEqual((fields['main'], fields['passState']), ('unchanged', 'indexed'))
        self.assertEqual(fields['linked']['pending'], 'linked_reoffer')
        self.assertEqual(fields['linkedViews'],
                         [{'view': worker._view_id('/private/side'), 'state': 'pending',
                           'reason': 'schema3_pass_budget'},
                          {'view': worker._view_id('/private/other'), 'state': 'indexed', 'vector': 'pending'}])
        self.assertNotIn('/private', str(fields))
        self.assertRegex(fields['linkedViews'][0]['view'], r'^[0-9a-f]{12}$')
        self.assertEqual(worker._pass_fields({'state': 'unchanged'}, 'unchanged'),
                         {'main': 'unchanged', 'passState': 'unchanged'})
