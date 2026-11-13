#!/usr/bin/env python3
"""Vector generations of a selected schema3 install whose view store is in the row format (4).

The U12e fixture (stub provider, actual sync pass, reseal and persisted
hybrid recall) with the seed's store created in the row format. A row head
fills its own eligible cards (design: fill cursor bound to the head), so a
pass whose new cards lack sets publishes the lexical generation first and
the vector generation after the fill; a pass whose sets are all stored
publishes one vector generation. No model loads.
"""
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_vectors', ROOT / 'test/fixtures/laya-schema3-vectors-checks.py')
vectors = importlib.util.module_from_spec(spec); spec.loader.exec_module(vectors)
spec = importlib.util.spec_from_file_location('s_sync_rows', ROOT / 'test/fixtures/laya-s-sync-rows-checks.py')
rows = importlib.util.module_from_spec(spec); spec.loader.exec_module(rows)


class RowVectorChecks(rows.RowStoreReads, vectors.Schema3VectorChecks):
    def coverage(self, gen_id):
        """``(spec or None, coverage)`` sealed by one row generation."""
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            recipe, profile, coverage = db.execute('SELECT recipe_id,profile_id,coverage FROM s_generations '
                                                   'WHERE gen_id=?', (gen_id,)).fetchone()
        return (None if coverage != 'complete' else dict(recipe_id=recipe, profile_id=profile)), coverage

    def test_31_lexical_then_vector_generation_in_one_pass_reseals_and_recall_is_hybrid(self):
        self.select_schema3()
        self.assertIsNone(self.selected_record()['vector_profile'])
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        start = self.head()
        result, _ingests, calls = self.run_vector_pass()
        self.assertEqual((result['state'], result['vector']['state'], result.get('readerResettle')),
                         ('indexed', 'published', 'resealed'), result)
        gen = self.head()
        # The lexical generation, then the head's own fill and its vector generation.
        self.assertEqual(gen, (result['generation'], start[1] + 2))
        self.assertEqual(self.coverage(gen[0]), (self.spec, 'complete'))
        eligible = self.eligible(gen[0])
        self.assertEqual(sorted(path for _kind, path in calls),
                         sorted('blobs/' + blob + '.md' for blob in set(eligible.values())))
        record = self.selected_record()
        self.assertEqual((record['vector_profile'], record['vector_recipe']),
                         (self.spec['profile_id'], self.spec['recipe_id']))
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        idle, idle_ingests, idle_calls = self.run_vector_pass()
        self.assertEqual((idle['state'], idle['vector']['state'], idle_ingests, idle_calls, self.head()),
                         ('unchanged', 'current', [], [], gen), idle)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        target = 'concepts/kestrel-perch.md'
        hit = self.hybrid_recall('zzqqxv nomatch', self.card_vector(gen[0], target))
        self.assertEqual((hit.store_format, hit.data_generation, hit.semantic_status),
                         ('schema3', gen[0], 'complete'))
        self.assertEqual(self.paths(hit)[0], target)
        vectors.emit('rows-vector-generation', providerCalls=len(calls), epochs=gen[1] - start[1])

    def test_32_resource_refusal_keeps_lexical_head_then_the_head_fill_seals_vectors(self):
        self.select_schema3()
        self.card('concepts/wren-path.md', 'wrenprobe')
        start = self.head()
        refused, _ingests, calls = self.run_vector_pass(vectors.Coordinator(refuse='cpu_pressure'))
        self.assertEqual((refused['state'], refused['reason'], refused['vector']),
                         ('deferred', 'cpu_pressure', {'state': 'deferred', 'reason': 'cpu_pressure'}),
                         refused)
        self.assertEqual(calls, [])
        lexical = self.head()
        self.assertEqual(lexical, (refused['generation'], start[1] + 1))
        self.assertEqual(self.coverage(lexical[0]), (None, 'lexical'))
        self.assertIsNone(self.selected_record()['vector_profile'])
        self.assertIn('concepts/wren-path.md', self.paths(self.recall('wrenprobe')))
        # The next pass finds nothing changed, fills the head and seals its vector generation.
        filled, again, calls = self.run_vector_pass()
        self.assertEqual((filled['state'], filled['vector']['state'], again, filled.get('readerResettle')),
                         ('indexed', 'published', [], 'resealed'), filled)
        self.assertEqual(self.head(), (filled['generation'], lexical[1] + 1))
        self.assertEqual(self.members(filled['generation']), self.members(lexical[0]))
        self.assertEqual(len(calls), len(set(self.eligible(filled['generation']).values())))
        # A lexical head under the vector-sealed record still answers (vectors pending).
        self.card('concepts/wren-path.md', 'robinprobe')
        pending, _ingests, _calls = self.run_vector_pass(vectors.Coordinator(refuse='memory_reserve'))
        self.assertEqual((pending['state'], pending['vector']['state']), ('deferred', 'deferred'), pending)
        hit = self.hybrid_recall('robinprobe', [1.0, 0.5, 0.25])
        self.assertEqual((hit.data_generation, hit.semantic_status), (pending['generation'], 'lexical_only'))
        self.assertIn('concepts/wren-path.md', self.paths(hit))

    def test_33_edit_back_to_stored_bytes_publishes_one_vector_generation_without_provider(self):
        self.select_schema3()
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        original = (self.wiki / 'concepts/heron-watch.md').read_bytes()
        first, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(first['vector']['state'], 'published', first)
        self.card('concepts/heron-watch.md', 'egretprobe')
        edited, ingests, calls = self.run_vector_pass()
        self.assertEqual((edited['state'], edited['vector']['state'], ingests),
                         ('indexed', 'published', ['concepts/heron-watch.md']), edited)
        self.assertEqual(len(calls), 1)
        before = self.head()
        (self.wiki / 'concepts/heron-watch.md').write_bytes(original)
        back, ingests, calls = self.run_vector_pass()
        self.assertEqual((back['state'], back['vector']['state'], ingests, calls),
                         ('indexed', 'published', ['concepts/heron-watch.md'], []), back)
        self.assertEqual(self.head(), (back['generation'], before[1] + 1))
        self.assertEqual(self.coverage(back['generation']), (self.spec, 'complete'))


for _name in dir(vectors.Schema3VectorChecks):
    if _name.startswith('test') and _name not in vars(RowVectorChecks):
        setattr(RowVectorChecks, _name, None)


if __name__ == '__main__':
    unittest.main()
