"""Synthetic compatible legacy vector copy and semantic parity for the private shadow."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
spec = importlib.util.spec_from_file_location('vector_seed', HERE / 'laya-reader-move-seal-checks.py')
seed_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_module)

# A card without a Markdown heading: the v6 title is its filename stem, the
# path-independent recipe's title is its blob SHA, so the inputs differ.
TITLE_ONLY = 'Kestrel plan'
# A card long enough for several first-pass parts whose v6 positions are that
# first pass (copied).
MULTI = 'Otter ledger'
LONG = ''.join('%03d the sealed river counts keep every otter crossing in the ledger\n' % n
               for n in range(90))


def synthetic_vector(path, seq=0):
    digest = hashlib.sha256((path if seq == 0 else '%s#%d' % (path, seq)).encode('utf8')).digest()
    return [0.1 + digest[index] / 255.0 for index in range(3)]


class ShadowVectorChecks(unittest.TestCase):
    def test_compatible_vectors_are_copied_and_compared(self):
        published = seed_module.base.published
        original_card = published.card

        def card(title, body):
            if title == MULTI:
                body += '\n' + LONG
            text = original_card(title, body)
            return text.replace('## Summary\n', '') if title == TITLE_ONLY else text
        views_vectors = importlib.import_module('laya_view_vectors')

        def embed(_provider, body, path):
            positions = [pos for pos, _text in views_vectors.qmd_first_pass(body)]
            return [{'seq': seq, 'pos': pos, 'vector': synthetic_vector(path, seq)}
                    for seq, pos in enumerate(positions)]

        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        seed = BaseOnly('runTest')
        with mock.patch.object(published, 'card', new=card), \
             mock.patch.object(published.FillProvider, 'embed_document_chunks', new=embed):
            seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        views = importlib.import_module('laya_view_store')
        legacy = importlib.import_module('laya_index_vector')
        migration = importlib.import_module('laya_shadow_migration')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = views.ViewStore.create(case.state / 'vector-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)  # Let freshly-created Git metadata leave its timestamp tick.
        profile = published.vector_profile()
        recipe_id = store.register_recipe({
            'input_recipe': 'blob-input-v1', 'tokenizer_sha': '1' * 64, 'chunker_sha': '2' * 64,
            'window_tokens': 900, 'overlap_tokens': 135, 'window_search_tokens': 200,
            'offset_unit': 'utf16-code-unit', 'heading_rule': 'qmd-markdown-v1',
            'document_template': 'title: {title} | text: {text}', 'synthetic_path': 'blobs/{blob_sha}.md'})
        def register(dimension, fingerprint):
            return store.register_profile({
                'model_id': profile['modelId'], 'model_sha': '3' * 64,
                'embedding_fingerprint': fingerprint, 'dimension': dimension, 'metric': 'cosine',
                'document_input_recipe': 'blob-input-v1', 'query_input_recipe': 'qmd-query-v1',
                'query_template': 'task: search result | query: {text}'})
        good = {'recipe_id': recipe_id, 'profile_id': register(profile['dimension'],
                                                                profile['embeddingFingerprint'])}
        wrong = {'recipe_id': recipe_id, 'profile_id': register(4, 'f' * 64)}
        intent_path = case.state / 'shadow-migration-intent.json'

        def counts():
            return {table: store._db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                    for table in ('chunk_sets', 'embedding_sets', 'vectors')}

        def step(vector_spec=None):
            return migration.advance_shadow(store, project=case.project, executing_root=case.plugin,
                                            deadline=time.monotonic() + 60, vector_spec=vector_spec)

        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 2):
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            # Provider absent: no vector spec, the lexical shadow still
            # reaches caught_up and its private head answers lexical search.
            stages = []
            for _ in range(40):
                result = step()
                stages.append(result['stage'])
                if result['migration_state'] == 'caught_up':
                    break
            self.assertEqual(result['migration_state'], 'caught_up', stages)
            self.assertNotIn('vectors', stages)
            generation = result['intent']['published']['generation']
            self.assertEqual(step()['stage'], 'parity')
            handle = store.capture_view(worktree['worktreeId'])
            pin = store.pin_view(handle)
            try:
                self.assertTrue(any(row['path'] == 'concepts/heron-route.md'
                                    for row in store.search(pin, 'heron', 10)))
            finally:
                store.release(pin)
            self.assertEqual(counts(), {'chunk_sets': 0, 'embedding_sets': 0, 'vectors': 0})

            # An incompatible profile defers every member without blocking
            # the caught-up lexical state.
            wrong_stages = []
            for _ in range(5):
                result = step(wrong)
                wrong_stages.append(result['stage'])
                if result['stage'] == 'parity':
                    break
            self.assertEqual(wrong_stages, ['vectors', 'vectors', 'parity'])
            record = result['intent']['vectors']
            self.assertEqual((record['total'], record['copied'], record['deferred']['profile_incompatible']),
                             (3, 0, 3))
            self.assertEqual(result['migration_state'], 'caught_up')
            self.assertEqual(counts(), {'chunk_sets': 0, 'embedding_sets': 0, 'vectors': 0})
            empty = migration.compare_vector_queries(store, generation, project=case.project,
                executing_root=case.plugin, vector_spec=wrong, query_vectors=((1.0, 0.0, 0.0, 0.0),),
                limit=2, deadline=time.monotonic() + 30)
            self.assertEqual((empty['subset'], empty['semantic_status']), (0, 'no_compatible_vectors'))
            with self.assertRaisesRegex(Exception, 'migration_vectors_incomplete'):
                migration.compare_vector_queries(store, generation, project=case.project,
                    executing_root=case.plugin, vector_spec=good, query_vectors=((1.0, 0.0, 0.0),),
                    limit=2, deadline=time.monotonic() + 30)

            # Forged v6 vector bytes or dimension are refused before any write.
            actual_read = legacy._read_chunk_set
            for forge in ('bytes', 'dimension'):
                def forged(*args, forge=forge, **kwargs):
                    rows = actual_read(*args, **kwargs)
                    if rows:
                        vector = list(rows[0]['vector'])
                        if forge == 'bytes':
                            vector[0] += 0.5
                        else:
                            vector.append(0.5)
                        rows[0] = {**rows[0], 'vector': vector}
                    return rows
                before = intent_path.read_bytes()
                with self.subTest(forge=forge), mock.patch.object(legacy, '_read_chunk_set', side_effect=forged):
                    with self.assertRaisesRegex(Exception, 'migration_vector_invalid'):
                        step(good)
                self.assertEqual(intent_path.read_bytes(), before)
                self.assertEqual(counts(), {'chunk_sets': 0, 'embedding_sets': 0, 'vectors': 0})

            # Exact-input vectors are copied; the title-only card is not.
            good_stages = []
            for _ in range(5):
                result = step(good)
                good_stages.append(result['stage'])
                if result['stage'] == 'parity':
                    break
            self.assertEqual(good_stages, ['vectors', 'vectors', 'parity'])
            record = result['intent']['vectors']
            self.assertTrue(record['complete'])
            self.assertEqual((record['total'], record['copied'], record['deferred']['input_differs']), (3, 2, 1))
            self.assertEqual(sum(record['deferred'].values()), 1)
            print('compat_ratio', '%d/%d' % (record['copied'], record['total']),
                  round(record['copied'] / record['total'], 3), flush=True)
            members = dict(store._db.execute('SELECT path,blob_sha FROM wiki_members WHERE gen_id=?',
                                             (generation,)).fetchall())
            self.assertEqual(len(members), 3)
            import struct
            multi_parts = views_vectors.qmd_first_pass(store.document(members['concepts/otter-ledger.md'])['body'])
            self.assertGreater(len(multi_parts), 1)
            self.assertEqual(counts(), {'chunk_sets': 2, 'embedding_sets': 2, 'vectors': 1 + len(multi_parts)})
            for rel, blob in members.items():
                copied = [tuple(row) for row in store._db.execute('SELECT embedding FROM vectors WHERE blob_sha=? '
                    'AND recipe_id=? AND profile_id=? ORDER BY seq', (blob, good['recipe_id'], good['profile_id']))]
                if rel.endswith('kestrel-plan.md'):
                    self.assertEqual(copied, [])
                    self.assertIsNone(store._db.execute('SELECT 1 FROM chunk_sets WHERE blob_sha=?',
                                                        (blob,)).fetchone())
                elif rel.endswith('otter-ledger.md'):
                    # The v6 positions are the first pass: its texts make the chunk set.
                    chunks = store.chunks(blob, good['recipe_id'])
                    self.assertEqual([(c['position'], c['text']) for c in chunks], multi_parts)
                    self.assertEqual(copied, [(struct.pack('<fff', *synthetic_vector(case.WIKI + '/' + rel, seq)),)
                                              for seq in range(len(multi_parts))])
                else:
                    expected = synthetic_vector(case.WIKI + '/' + rel)
                    self.assertEqual(copied, [(struct.pack('<fff', *expected),)])
            # v6 positions the provider re-split (not its first pass) are not
            # copied even when writing: that card's chunk set is the fill's.
            kestrel = members['decisions/kestrel-plan.md']
            before = counts()
            resplit = (TITLE_ONLY, [{'seq': 0, 'pos': 0, 'embedding': struct.pack('<fff', 1, 0, 0)},
                                    {'seq': 1, 'pos': 5, 'embedding': struct.pack('<fff', 0, 1, 0)}])
            stored_profile = store._profile(good['profile_id'])
            with mock.patch.object(migration, '_legacy_vector_set', return_value=resplit):
                page = migration._classify_vectors(
                    store, None, scope=None, generation=generation, members=[('decisions/kestrel-plan.md', kestrel)],
                    names=[case.WIKI], seal=stored_profile, profile=stored_profile, spec=good,
                    deadline=time.monotonic() + 30, write=True)
            self.assertEqual([(row['status'], row['reason']) for row in page],
                             [('budget-deferred', 'multi_chunk_unreconstructable')])
            self.assertEqual(counts(), before)
            completed = intent_path.read_bytes()
            again = migration.advance_vectors(store, project=case.project, executing_root=case.plugin,
                                              vector_spec=good, deadline=time.monotonic() + 30)
            self.assertEqual(again['vectors'], record)
            self.assertEqual(intent_path.read_bytes(), completed)

            # Same query vectors: old pinned v6 exact search and the copied
            # candidate sets agree on the compatible subset.
            queries = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.2, 0.3, 1.0), (-1.0, 0.5, 0.25))
            for limit in (1, 2, 3):
                compared = migration.compare_vector_queries(store, generation, project=case.project,
                    executing_root=case.plugin, vector_spec=good, query_vectors=queries,
                    limit=limit, deadline=time.monotonic() + 30)
                self.assertEqual(compared, {'subset': 2, 'total': 3, 'compared': len(queries),
                                            'semantic_status': 'complete'})
            actual_search = legacy.LayaVectorIndex.search_exact
            def reversed_search(*args, **kwargs):
                return list(reversed(actual_search(*args, **kwargs)))
            with mock.patch.object(legacy.LayaVectorIndex, 'search_exact', autospec=True,
                                   side_effect=reversed_search):
                with self.assertRaisesRegex(Exception, 'migration_semantic_parity_mismatch'):
                    migration.compare_vector_queries(store, generation, project=case.project,
                        executing_root=case.plugin, vector_spec=good, query_vectors=((1.0, 0.0, 0.0),),
                        limit=2, deadline=time.monotonic() + 30)
            for forged_query in ((1.0, 0.0, 0.0, 0.0), (float('nan'), 0.0, 0.0), (0.0, 0.0, 0.0)):
                with self.subTest(query=forged_query):
                    with self.assertRaisesRegex(Exception, 'vector_query_invalid'):
                        migration.compare_vector_queries(store, generation, project=case.project,
                            executing_root=case.plugin, vector_spec=good, query_vectors=(forged_query,),
                            limit=2, deadline=time.monotonic() + 30)
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            self.assertEqual(json.loads(intent_path.read_text())['migration_state'], 'caught_up')
            self.assertTrue(store.verify_views())
            events = [line for line in (case.state / 'events.jsonl').read_text(encoding='utf8').splitlines()
                      if json.loads(line).get('event') == 'migration.shadow']
            self.assertTrue(any('"vectors"' in line for line in events))
            self.assertFalse(any('heron' in line or 'otter' in line for line in events))



class VectorStepSetup:
    """The vectors fixture of ``ShadowVectorChecks``: seed, private store, recipe, profile."""

    def build(self, *, extra_cards=0):
        published = seed_module.base.published
        original_card = published.card

        def card(title, body):
            if title == MULTI:
                body += '\n' + LONG
            text = original_card(title, body)
            return text.replace('## Summary\n', '') if title == TITLE_ONLY else text
        views_vectors = importlib.import_module('laya_view_vectors')

        def embed(_provider, body, path):
            positions = [pos for pos, _text in views_vectors.qmd_first_pass(body)]
            return [{'seq': seq, 'pos': pos, 'vector': synthetic_vector(path, seq)}
                    for seq, pos in enumerate(positions)]

        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        seed = BaseOnly('runTest')
        with mock.patch.object(published, 'card', new=card), \
             mock.patch.object(published.FillProvider, 'embed_document_chunks', new=embed):
            seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        views = importlib.import_module('laya_view_store')
        self.migration = importlib.import_module('laya_shadow_migration')
        self.blobs = importlib.import_module('laya_blob_store')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = views.ViewStore.create(case.state / 'vector-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)  # Let freshly-created Git metadata leave its timestamp tick.
        profile = published.vector_profile()
        recipe_id = store.register_recipe({
            'input_recipe': 'blob-input-v1', 'tokenizer_sha': '1' * 64, 'chunker_sha': '2' * 64,
            'window_tokens': 900, 'overlap_tokens': 135, 'window_search_tokens': 200,
            'offset_unit': 'utf16-code-unit', 'heading_rule': 'qmd-markdown-v1',
            'document_template': 'title: {title} | text: {text}', 'synthetic_path': 'blobs/{blob_sha}.md'})
        good = {'recipe_id': recipe_id, 'profile_id': store.register_profile({
            'model_id': profile['modelId'], 'model_sha': '3' * 64,
            'embedding_fingerprint': profile['embeddingFingerprint'], 'dimension': profile['dimension'],
            'metric': 'cosine', 'document_input_recipe': 'blob-input-v1', 'query_input_recipe': 'qmd-query-v1',
            'query_template': 'task: search result | query: {text}'})}
        self.seed, self.case, self.store, self.good = seed, case, store, good
        self.worktree = worktree
        return seed, case, store, good

    def counts(self):
        return {table: self.store._db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('chunk_sets', 'embedding_sets', 'vectors')}

    @staticmethod
    def counting(target, name):
        """Record each call's arguments (without ``self`` for a class method)."""
        real = getattr(target, name)
        calls, method = [], isinstance(target, type)

        def wrapper(*args, **kwargs):
            calls.append(args[1:] if method else args)
            return real(*args, **kwargs)
        return calls, mock.patch.object(target, name, new=wrapper)


class VectorPageChecks(VectorStepSetup, unittest.TestCase):
    """One vectors page of 100 actual admitted cards in the actual candidate store.

    Only the v6 side (``_legacy_vector_set``) is synthetic: one exact-input
    chunk per card at position 0, as the provider's first pass gives a short
    card.
    """
    PAGE = 100

    def setUp(self):
        _seed, case, store, good = self.build()
        wiki = case.auto / 'wiki' / 'many'
        wiki.mkdir(parents=True, exist_ok=True)
        self.members = []
        for number in range(self.PAGE):
            rel = 'many/card-%03d.md' % number
            (case.auto / 'wiki' / rel).write_text('# Card %d\n\nPage row %d of the vectors fixture.\n'
                                                  % (number, number), encoding='utf8')
        for number in range(self.PAGE):
            rel = 'many/card-%03d.md' % number
            self.members.append((rel, store.ingest_card(self.worktree['worktreeId'], rel)))
        self.profile = store._profile(good['profile_id'])
        values = importlib.import_module('laya_blob_values')
        import struct
        titles = {blob: values.markdown_title(store.document(blob)['body'], blob)
                  for _rel, blob in self.members}
        dimension = self.profile['dimension']

        def legacy(_base, *, blob, **_kwargs):
            return titles[blob], [{'seq': 0, 'pos': 0,
                                   'embedding': struct.pack('<' + 'f' * dimension,
                                                            *([0.5] * dimension))}]
        patch = mock.patch.object(self.migration, '_legacy_vector_set', side_effect=legacy)
        patch.start()
        self.addCleanup(patch.stop)

    def classify(self, members, **kwargs):
        return self.migration._classify_vectors(
            self.store, None, scope=None, generation='0' * 32, members=members,
            names=[self.case.WIKI], seal=self.profile, profile=self.profile, spec=self.good,
            deadline=time.monotonic() + 60, write=True, **kwargs)


    def test_01_page_proves_store_invariants_once(self):
        headers, header_patch = self.counting(self.blobs, '_header')
        blobs, blob_patch = self.counting(self.blobs.BlobStore, '_blob')
        texts, texts_patch = self.counting(self.blobs.BlobStore, '_input_texts')
        with header_patch, blob_patch, texts_patch:
            page = self.classify(self.members)
        print('vector_page_calls', json.dumps({'header': len(headers), 'blob': len(blobs),
                                                'input_texts': len(texts)}), flush=True)
        self.assertEqual([row['status'] for row in page], ['copied'] * self.PAGE)
        self.assertLessEqual(len(headers), 2)
        self.assertLessEqual(len(blobs), self.PAGE)
        self.assertEqual(len(texts), self.PAGE)
        self.assertEqual(self.counts(), {'chunk_sets': self.PAGE, 'embedding_sets': self.PAGE,
                                         'vectors': self.PAGE})

    def test_02_budget_stops_after_a_member_and_the_next_page_resumes_after_it(self):
        puts, put_patch = self.counting(self.blobs.BlobStore, 'put_embedding_set')
        with put_patch, mock.patch.object(self.migration, '_out_of_budget',
                                          side_effect=lambda _stop: len(puts) >= 50):
            page = self.classify(self.members, stop_at=time.monotonic() + 60)
        self.assertEqual(len(page), 50)
        self.assertEqual(page[-1]['path'], self.members[49][0])
        self.assertEqual(self.counts()['embedding_sets'], 50)
        del puts[:]
        with put_patch:
            rest = self.classify(self.members[50:])
        self.assertEqual(puts[0][0], self.members[50][1])
        self.assertEqual([row['path'] for row in rest], [rel for rel, _blob in self.members[50:]])
        self.assertEqual(self.counts()['embedding_sets'], self.PAGE)

    def test_03_member_that_runs_out_of_time_is_rolled_back_alone(self):
        real_put, calls = self.blobs.BlobStore.put_embedding_set, []

        def put(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 51:
                raise self.migration.IndexContractError('reader_snapshot_deadline')
            return real_put(store, *args, **kwargs)
        with mock.patch.object(self.blobs.BlobStore, 'put_embedding_set', new=put):
            page = self.classify(self.members, stop_at=time.monotonic() + 60)
        self.assertEqual(len(page), 50)
        # The 51st member's chunk set (written before its embedding set) is gone too.
        self.assertEqual(self.counts(), {'chunk_sets': 50, 'embedding_sets': 50, 'vectors': 50})
        # Without a budget the same failure fails the whole page, as before.
        calls.clear()
        with mock.patch.object(self.blobs.BlobStore, 'put_embedding_set', new=put):
            with self.assertRaisesRegex(Exception, 'reader_snapshot_deadline'):
                self.classify(self.members[50:] + self.members[:50])
        self.assertEqual(self.counts(), {'chunk_sets': 50, 'embedding_sets': 50, 'vectors': 50})

    def test_04_partial_sets_left_by_a_failed_page_are_continued(self):
        # A failed page of an earlier release committed member by member: 26
        # sets exist, the cursor never moved. The next page verifies those
        # through the same single session and copies the rest.
        self.classify(self.members[:26])
        self.assertEqual(self.counts()['embedding_sets'], 26)
        headers, header_patch = self.counting(self.blobs, '_header')
        blobs, blob_patch = self.counting(self.blobs.BlobStore, '_blob')
        with header_patch, blob_patch:
            page = self.classify(self.members)
        self.assertEqual([row['status'] for row in page], ['copied'] * self.PAGE)
        self.assertLessEqual(len(headers), 2)
        self.assertLessEqual(len(blobs), self.PAGE)
        self.assertEqual(self.counts(), {'chunk_sets': self.PAGE, 'embedding_sets': self.PAGE,
                                         'vectors': self.PAGE})


class VectorStepChecks(VectorStepSetup, unittest.TestCase):
    """``advance_vectors`` records progress at the last finished member."""

    def test_budget_inside_a_page_records_the_last_finished_member(self):
        seed, case, store, good = self.build()
        migration = self.migration
        views = importlib.import_module('laya_view_store')
        intent_path = case.state / 'shadow-migration-intent.json'

        def step(vector_spec=None):
            return migration.advance_shadow(store, project=case.project, executing_root=case.plugin,
                                            deadline=time.monotonic() + 60, vector_spec=vector_spec)
        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 3):
            migration.begin_migration(store, worktree_id=self.worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            for _ in range(40):
                result = step()
                if result['migration_state'] == 'caught_up':
                    break
            self.assertEqual(result['migration_state'], 'caught_up')
            members = [row[0] for row in store._db.execute(
                'SELECT path FROM wiki_members WHERE gen_id=? AND eligible=1 ORDER BY path',
                (result['intent']['published']['generation'],))]
            self.assertEqual(len(members), 3)
            puts, put_patch = self.counting(self.blobs.BlobStore, 'put_embedding_set')
            observes, observe_patch = self.counting(views.ViewStore, '_observe')
            with put_patch, observe_patch, mock.patch.object(
                    migration, '_out_of_budget', side_effect=lambda _stop: len(puts) >= 1):
                first = migration.advance_vectors(store, project=case.project, executing_root=case.plugin,
                                                  vector_spec=good, deadline=time.monotonic() + 60)
            record = first['vectors']
            self.assertEqual((record['cursor'], record['copied'], record['complete']),
                             (members[0], 1, False))
            self.assertEqual(json.loads(intent_path.read_text())['vectors'], record)
            # One closing checkout observation per step (the opening one is capture_view).
            self.assertLessEqual(len(observes), 1)
            del puts[:]
            with put_patch:
                second = migration.advance_vectors(store, project=case.project, executing_root=case.plugin,
                                                   vector_spec=good, deadline=time.monotonic() + 60)
            blobs = dict(store._db.execute('SELECT path,blob_sha FROM wiki_members WHERE gen_id=?',
                                           (result['intent']['published']['generation'],)).fetchall())
            self.assertEqual(puts[0][0], blobs[members[1]])
            final = second['vectors']
            self.assertTrue(final['complete'])
            self.assertEqual((final['total'], final['copied'], final['deferred']['input_differs']), (3, 2, 1))
            self.assertEqual(self.counts()['embedding_sets'], 2)

if __name__ == '__main__':
    unittest.main()
