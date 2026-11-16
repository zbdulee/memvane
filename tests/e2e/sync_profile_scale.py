"""Scale experiment: real view store/fill, synthetic cards and embedding provider.

Run explicitly under the development guard. Seeding is outside the measured
pass. The provider returns seven deterministic vectors per document; it does
not model GPU latency. Route and owner are fixture seams, never live state.
"""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memvane.store.tests import support as seed
from memvane.sync import product_sync_worker as worker
from memvane.store import api as store_api

CARDS = 3751
CHUNKS = 7


class Scale(seed.FillCase):
    def test_large_view_fill_profile(self):
        store, _repository, handle = self.seeded()
        manifest = self.snapshot(store, handle)
        for index in range(1, CARDS):
            self.add_member(store, handle, manifest, 'cards/%04d.md' % index,
                            ('# Card %04d\nsynthetic\n' % index).encode(), 'Card %04d' % index)
        recipe = store.register_recipe(seed.recipe())
        profile_id = store.register_profile(seed.profile())
        self.lexical(store, handle, manifest)

        def parts(body, *_args):
            found = []
            for i, char in enumerate(body):
                if char not in {text for _pos, text in found}:
                    found.append((i, char))
                if len(found) == CHUNKS:
                    return found
            raise AssertionError('synthetic body needs seven distinct characters')

        class Provider:
            def embed_document_chunks(self, body, path):
                return [{'pos': pos, 'vector': [1.0, 0.5]} for pos, _ in parts(body)]

            def embed_document(self, body, title):
                return [1.0, 0.5]

        target = self.state / 'events'
        target.mkdir(mode=0o700)
        decision = {'projectRoot': str(self.root), 'journalPath': str(target / 'journal.sqlite'),
                    'installId': '1' * 32}
        proof = {}
        result_holder = {}

        def run_schema3(_decision, _proof, *, profile, **_kwargs):
            # This exercises the real scan, row fill planning/coverage and fill
            # callbacks. The surrounding selected-route proof is a fixture.
            worker._profile_call(profile, 'scan', worker._schema3_wiki_cards, str(self.wiki()))
            vectors = {'spec': {'recipe_id': recipe, 'profile_id': profile_id},
                       'acquire': lambda _proof: Provider(), 'profile': profile, 'stash': {}}
            result = worker._schema3_vectors(store, None, proof, {}, handle, None, None, vectors,
                                             time.monotonic() + 240, window=True)
            result_holder.update(result)
            if result['state'] == 'complete':
                head = store.head(handle)
                deadline = time.monotonic() + 240
                generation = worker._profile_call(profile, 'publish', store.prepare_generation, handle,
                                                  manifest, expected_head=head, vector_spec=vectors['spec'],
                                                  deadline=deadline)
                worker._profile_call(profile, 'publish', store.publish_generation, handle, generation,
                                     expected_head=head, deadline=deadline)
            return {'state': 'unchanged', 'vector': result}, proof

        patches = [mock.patch.object(worker, '_prepare_provider_reattest'),
                   mock.patch.object(worker, '_selected', return_value=(decision, proof)),
                   mock.patch.object(worker, '_route_source', return_value='fixture'),
                   mock.patch.object(worker, '_run_schema3', side_effect=run_schema3),
                   mock.patch.object(worker, '_after_sync', side_effect=lambda _d, _p, _g, result, **_k: result),
                   mock.patch.object(worker, '_mark_schema3_write'),
                   mock.patch.object(store_api, 'qmd_first_pass', side_effect=parts),
                   mock.patch('memvane.runtime.api.request', return_value={'status': 'queued'})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        worker._run_sync(self.root)
        lines = [json.loads(line) for line in (target / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(len(lines), 1)
        event = lines[0]
        self.assertEqual(event['event'], 'sync.profile')
        self.assertEqual(len(manifest['members']), CARDS)
        rows = store._db.execute('SELECT count(*), sum(chunk_count) FROM embedding_sets').fetchone()
        self.assertGreater(rows[0], 0)
        self.assertEqual(rows[1], rows[0] * CHUNKS)
        self.assertGreater(event['providerCalls'], 0)
        self.assertGreater(event['coverageCalls'], 0)
        self.assertGreater(event['planCalls'], event['storageCalls'])
        self.assertEqual(result_holder['state'], 'complete')
        self.assertEqual(event['publishCalls'], 2)
        print('SCALE_PROFILE ' + json.dumps({'cards': CARDS, 'chunksPerCard': CHUNKS,
              'setsFilled': rows[0], 'vectorRows': rows[1], 'result': result_holder.get('state'),
              'event': event}, sort_keys=True), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
