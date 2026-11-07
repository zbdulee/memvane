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


if __name__ == '__main__':
    unittest.main()
