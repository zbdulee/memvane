#!/usr/bin/env python3
"""S view publication contract on an isolated, synthetic ViewStore."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('s_generation_fixture', HERE / 'laya-view-generations-checks.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


class IncrementalPublicationChecks(seed.GenerationCase):
    def test_one_card_change_reuses_view_fts_and_does_not_persist_full_stage(self):
        store, _, handle = self.seeded()
        first = self.snapshot(store, handle, body=b'# First\nuniquealpha synthetic card\n')
        initial = store.head(handle)
        first_gen = store.prepare_generation(handle, first, expected_head=initial)
        first_head = store.publish_generation(handle, first_gen, expected_head=initial)
        first_fts = self.fts_tables(store)
        self.assertEqual(len(first_fts), 1, first_fts)

        second = self.snapshot(store, handle, body=b'# Second\nuniquebeta synthetic card\n')
        next_gen = store.prepare_generation(handle, second, expected_head=first_head)
        next_head = store.publish_generation(handle, next_gen, expected_head=first_head)
        self.assertEqual(next_head['epoch'], first_head['epoch'] + 1)
        pin = store.pin_view(handle)
        try:
            self.assertEqual([row['path'] for row in store.search(pin, 'uniquebeta', 10)], ['cards/a.md'])
            self.assertEqual(store.search(pin, 'uniquealpha', 10), [])
        finally:
            store.release(pin)
        self.assertEqual(self.fts_tables(store), first_fts,
                         'each view keeps one head FTS instead of one FTS per generation')
        tables = {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'generation_slots' in tables:
            fields = {row[1] for row in store._db.execute('PRAGMA table_info(generation_slots)')}
            self.assertNotIn('stage_json', fields, 'full manifest/files must not accumulate per generation')

    @staticmethod
    def fts_tables(store):
        return {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND upper(sql) LIKE '%USING FTS5%'").fetchall()}


if __name__ == '__main__':
    unittest.main()
