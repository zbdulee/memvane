"""Sync worker rules that need no install: budgets, codes, route sources, the view chunker."""
import unittest

from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.store.api import qmd_first_pass
from memvane.store.types import FALLBACK_CHARS
from memvane.sync import product_sync_worker as worker


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
