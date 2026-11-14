"""Main-checkout recall over the journal-selected schema3 view.

Recall reads the published view only: no raw source file is opened, a branch
without a published head answers ``view_preparing`` without context (never the
main view's rows), a checkout that keeps moving answers ``checkout_changed``,
and a hot store journal is a recovery refusal that asks for the sync owner.
A reader sealed without a vector spec serves lexical rows without asking the
provider.
"""
from contextlib import closing
import json
import os
import sqlite3
import sys
import threading
import unittest
from unittest import mock

from memvane.recall import api as recall_api
from memvane.recall.types import HostResponseError
from memvane.recall.tests.support import NOTE, RecallInstallCase
from tests.support.project import settle_index
from tests.support.sqlite_hot_journal import kill_mid_write

HOSTS = ('claude', 'codex', 'hermes')
PROMPT = 'what marker does the heron protocol keep?'
_OPENED = []


def _audit(event, args):
    if event == 'open' and _OPENED and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED[-1].append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


class CheckoutMoved(Exception):
    reason = 'checkout_changed'


class Schema3Recall(RecallInstallCase):
    CARDS = 2

    def opened(self, call):
        _OPENED.append([])
        try:
            return call(), _OPENED[-1]
        finally:
            _OPENED.pop()

    def generation(self, context):
        header = context.split('\n', 1)[0]
        self.assertIn('generation ', header)
        return header.split('generation ', 1)[1].split(';', 1)[0]

    def test_main_recall_reads_the_published_view_for_every_host(self):
        contexts = {}
        for host in HOSTS:
            with self.subTest(host=host):
                (context, err, event), opened = self.opened(lambda: self.recall(PROMPT, host))
                contexts[host] = context
                self.assertIn('zqheronx', context)
                self.assertNotIn('zqglacierx', context)
                self.assertEqual([path for path in opened if path.endswith('notes.md')], [])
                self.assertEqual((event['outcome'], event['store_format']), ('served', 'schema3'))
                self.assertTrue(event['reader_endpoint'])
                self.assertEqual(event['selection_fallback'], 'laya_selector_not_prepared')
                self.assertEqual(event['vector_fallback'], 'selected_provider_resource_deferred')
                self.assertEqual(event['notes'], ['laya_vector_selected_provider_resource_deferred'])
                self.assertIn('selected_provider_resource_deferred; serving lexical results', err)
        self.assertEqual(len(set(contexts.values())), 1, 'every host renders the same context')
        self.assertEqual(self.generation(contexts['claude']), event['generation'])

    def test_refused_query_admission_serves_lexical_rows_with_one_owner_wake(self):
        results = []
        real = recall_api.query_endpoint

        def observed(*args, **kwargs):
            results.append(real(*args, **kwargs))
            return results[-1]
        with mock.patch('memvane.recall.api.query_endpoint', side_effect=observed):
            context, _err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual([result.semantic_status for result in results], ['provider_absent'])
        self.assertEqual(event['vector_fallback'], 'selected_provider_resource_deferred')
        self.assertEqual({(kind, target) for kind, target, _kwargs in self.wakes},
                         {('laya-sync', str(self.project))})

    def forge_reader_without_vector_spec(self):
        """The selected reader record as a lexical-first seal leaves it: no vector spec."""
        from memvane.journal.api import inspect_journal
        record = dict(inspect_journal(self.install.journal)['reader'],
                      vector_profile=None, vector_recipe=None)
        raw = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False)
        with closing(sqlite3.connect(str(self.install.journal), isolation_level=None)) as db:
            db.execute('UPDATE reader SET record=? WHERE id=1', (raw,))

    def test_a_view_sealed_without_vectors_serves_lexical_rows_without_asking_the_provider(self):
        self.install.provider.admit_queries()
        self.forge_reader_without_vector_spec()
        asked, reads = [], []
        real = recall_api.query_endpoint

        def observed(*args, **kwargs):
            reads.append((kwargs['query_vector'], real(*args, **kwargs)))
            return reads[-1][1]
        with mock.patch('memvane.recall.api.query_endpoint', side_effect=observed), \
                mock.patch('memvane.recall.api.approved_query_vector',
                           side_effect=lambda **kwargs: asked.append(kwargs)):
            context, err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual(asked, [])
        self.assertEqual([(vector, result.semantic_status) for vector, result in reads],
                         [(None, 'provider_absent')])
        self.assertEqual((event['outcome'], event['vector_fallback']),
                         ('served', 'selected_view_vectors_pending'))
        self.assertEqual(event['notes'], ['laya_vector_selected_view_vectors_pending'])
        self.assertIn('selected_view_vectors_pending; serving lexical results', err)
        self.assertEqual(self.wakes, [])

    def test_the_learning_selector_receives_the_published_rows(self):
        seen = []

        def select(pool, **_kwargs):
            seen.append([row['path'] for row in pool])
            return {'reason': 'selected', 'final_results': pool[:1]}
        with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                mock.patch('memvane.learning.api.select_published', side_effect=select):
            context, _err, event = self.recall(PROMPT)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0] and all(path.endswith('.md') and 'concepts/' in path
                                        for path in seen[0]), seen)
        self.assertIn('local Laya model selected these cards', context)
        self.assertEqual(event['count'], 1)
        self.assertIsNone(event.get('selection_fallback'))

    def test_a_new_branch_without_a_published_view_answers_preparing_without_context(self):
        self.fixture.git('checkout', '-q', '-b', 'cold-side')
        settle_index(self.project)
        for host in HOSTS:
            with self.subTest(host=host):
                self.wakes.clear()
                context, err, event = self.recall(PROMPT, host)
                self.assertEqual(context, '')
                self.assertEqual((event['outcome'], event['reason']), ('empty', 'view_preparing'))
                self.assertIn('laya_view_preparing', event.get('notes') or [])
                self.assertNotIn('request refused', err)
                self.assertEqual({(kind, target) for kind, target, _kwargs in self.wakes},
                                 {('laya-sync', str(self.project))})

    def test_a_checkout_that_keeps_moving_answers_without_context(self):
        real = recall_api.query_endpoint
        for moves, served in ((1, True), (2, False)):
            with self.subTest(moves=moves):
                left = [moves]

                def moving(*args, **kwargs):
                    if left[0]:
                        left[0] -= 1
                        raise CheckoutMoved('checkout_changed')
                    return real(*args, **kwargs)
                with mock.patch('memvane.recall.api.query_endpoint', side_effect=moving):
                    context, err, event = self.recall(PROMPT)
                if served:
                    self.assertIn('zqheronx', context)
                else:
                    self.assertEqual(context, '')
                    self.assertEqual((event['outcome'], event['reason']), ('empty', 'checkout_changed'))
                    self.assertNotIn('request refused', err)

    def test_recalls_during_a_publish_each_serve_the_old_or_the_new_view(self):
        old = self.generation(self.recall(PROMPT)[0])
        self.write_card('glacier')
        self.fixture.write_note('notes.md', NOTE + 'The heron also keeps zqheronx near the glacier.\n')
        self.fixture.commit('second card')
        done = threading.Event()
        errors = []

        def publish():
            try:
                self.install.settle()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                done.set()
        worker = threading.Thread(target=publish)
        worker.start()
        seen = []
        try:
            while not done.is_set() and len(seen) < 50:
                try:
                    context = self.recall(PROMPT)[0]
                except HostResponseError as exc:
                    # Two changes inside one recall (a retry already spent):
                    # refused, never served from a third or mixed view.
                    seen.append(str(exc))
                    continue
                seen.append(self.generation(context) if context else None)
        finally:
            worker.join(120)
        self.assertEqual(errors, [])
        new = self.generation(self.recall(PROMPT)[0])
        self.assertNotEqual(new, old)
        self.assertLessEqual(set(seen), {old, new, 'selected_proof_changed'}, seen)

    def test_a_hot_store_journal_is_rolled_back_before_the_reader_serves(self):
        # A writer killed mid-commit leaves a torn store with a hot journal right
        # before the reader opens. The recall's own route proof recovers it
        # (as route classification does, tests/e2e): the uncommitted
        # transaction rolls back and the published card serves.
        from memvane.journal import api as journal_api
        store = self.install.store_path
        real = journal_api.resolve_endpoint
        torn = {}

        def resolve(*args, **kwargs):
            if not torn:
                _before, torn['journal'] = kill_mid_write(store)
            return real(*args, **kwargs)
        with mock.patch('memvane.journal.api.resolve_endpoint', side_effect=resolve):
            context = self.recall(PROMPT, session='hot')[0]
        self.assertIn('zqheronx', context)
        self.assertFalse(torn['journal'].exists())
        with self.install.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='hot_probe'")
                             .fetchone()[0], 0)

if __name__ == '__main__':
    unittest.main()
