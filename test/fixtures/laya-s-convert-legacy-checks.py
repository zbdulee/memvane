#!/usr/bin/env python3
"""Legacy-imported generations convert to the row format with the same recall."""
import importlib.util
from pathlib import Path
import sqlite3
import time
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('schema3_journal_fixture', HERE / 'laya-journal-schema3-checks.py')
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)


class LegacyConvertChecks(journal.Schema3EndpointChecks):
    def recall(self, store, handle, query='heron'):
        pin = store.pin_view(handle)
        try:
            out = {}
            try:
                origins = {None: store.search(pin, query, 20)}
            except Exception as exc:
                if getattr(exc, 'reason', None) != 'migration_lexical_sidecar_unproved':
                    raise
                origins = {origin: store.search(pin, query, 20, _legacy_origin=origin)
                           for origin in ('base', 'sidecar')}
            for origin, rows in origins.items():
                out[origin] = [(r['path'], r['score'], r['display_title'], r['blob_sha'],
                                r.get('source_locations')) for r in rows]
            paths = sorted({row[0] for rows in out.values() for row in rows})
            out['locations'] = store.card_locations(pin, paths)
            return out
        finally:
            store.release(pin)

    def convert(self):
        with self.writer() as view:
            before = self.recall(view, view.capture_view(self.worktree_id))
            head = view.head(view.capture_view(self.worktree_id))
            view.convert_to_row_format()
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        return store, store.capture_view(self.worktree_id), before, head

    def test_legacy_head_converts_with_identical_recall_and_locations(self):
        self.publish()
        store, handle, before, head = self.convert()
        self.assertTrue(any(before[key] for key in before if key != 'locations'), before)
        self.assertEqual(store.head(handle), head)
        kind = store._db.execute('SELECT kind FROM s_generations WHERE gen_id=?', (head['gen_id'],)).fetchone()[0]
        self.assertEqual(kind, 'legacy')
        self.assertGreater(store._db.execute('SELECT COUNT(*) FROM s_legacy_fts').fetchone()[0], 0)
        self.assertEqual(self.recall(store, handle), before)

    def test_head_legacy_corpus_rows_survive_collection_and_refuse_deletion(self):
        self.publish()
        store, handle, before, _ = self.convert()
        count = store._db.execute('SELECT COUNT(*) FROM s_legacy_fts').fetchone()[0]
        while any(store.collect_view_rows(handle, limit=64).get(t) for t in ('s_cards', 's_legacy_fts', 'stage')):
            pass
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_legacy_fts').fetchone()[0], count)
        self.assertEqual(self.recall(store, handle), before)
        store.close()
        db = sqlite3.connect(str(self.store_path))
        self.addCleanup(db.close)
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'view_row_retained'):
            db.execute('DELETE FROM s_legacy_fts')
        db.rollback()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_legacy_fts').fetchone()[0], count)

    def test_normal_publication_over_a_legacy_head_refuses_for_now(self):
        self.publish()
        store, handle, _, head = self.convert()
        with self.assertRaises(Exception) as caught:
            store.prepare_generation(handle, {'scan_id': 'a' * 32, 'policy_digest': handle.policy_digest,
                                              'revocation_epoch': 0, 'complete': True, 'sources': [],
                                              'members': []}, expected_head=head)
        self.assertEqual(getattr(caught.exception, 'reason', None), 'row_format_unsupported')
        self.assertEqual(store.head(handle), head)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(LegacyConvertChecks):
        if name in LegacyConvertChecks.__dict__:
            suite.addTest(LegacyConvertChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
