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

    def shaped(self, store, handle, deadline_seconds=20):
        import time
        pin = store.pin_view(handle)
        try:
            with store._reading(time.monotonic() + deadline_seconds) as db:
                generation, seal, _, stage = store._read_pinned(db, pin, time.monotonic() + deadline_seconds)
            legacy = stage.get('legacy') or {}
            keys = ('observation', 'corpus_observation', 'scan', 'paths', 'fts_paths', 'active_fts', 'corpus',
                    'fts_sources', 'sidecar_corpus', 'sidecar_observation', 'sidecar_commitment')
            return (generation['state'], stage.get('vector_spec'), {k: legacy.get(k) for k in keys},
                    store._eligible_members(pin.gen_id), store._eligible_count(pin.gen_id),
                    store._preverify_view(handle, deadline=time.monotonic() + deadline_seconds),
                    pin.gen_id, store._pin_fts_table(pin, sidecar=False))
        finally:
            store.release(pin)

    def test_shadow_shaped_reads_match_across_the_conversion(self):
        self.publish()
        with self.writer() as view:
            before = self.shaped(view, view.capture_view(self.worktree_id))
            view.convert_to_row_format()
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        after = self.shaped(store, store.capture_view(self.worktree_id))
        import laya_view_fts as fts
        self.assertEqual(after[:7], before[:7])
        self.assertEqual(after[7], fts.head_table(store.capture_view(self.worktree_id).view_id))
        self.assertIsNotNone(after[2]['observation'])

    def normal_manifest(self, store, handle):
        """The head's cards as an ordinary manifest (their legacy_verified trust kept)."""
        rows = store._db.execute('SELECT path,blob_sha,title,trust_digest FROM s_cards '
                                 'WHERE view_id=? AND gen_to IS NULL ORDER BY path', (handle.view_id,)).fetchall()
        return {'scan_id': 'a' * 32, 'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                'complete': True, 'sources': [],
                'members': [dict(path=r[0], blob_sha=r[1], display_title=r[2], trust_digest=r[3]) for r in rows]}

    def publish_normal(self, store, handle, head, manifest):
        gen = store.prepare_generation(handle, manifest, expected_head=head)
        return store.publish_generation(handle, gen, expected_head=head)

    def test_normal_publication_over_a_legacy_head_closes_the_corpus_and_rolls_back(self):
        self.publish()
        store, handle, before, head = self.convert()
        import laya_view_fts as fts
        manifest = self.normal_manifest(store, handle)
        normal = self.publish_normal(store, handle, head, manifest)
        db = store._db
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_legacy_fts WHERE view_id=? AND gen_to IS NULL',
                                    (handle.view_id,)).fetchone()[0], 0)
        self.assertEqual(tuple(db.execute('SELECT kind,sidecar_present FROM s_generations WHERE gen_id=?',
                                          (normal['gen_id'],)).fetchone()), ('normal', 0))
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn(fts.head_table(handle.view_id, sidecar=True), tables)
        self.assertGreater(db.execute("SELECT COUNT(*) FROM s_deltas WHERE ordinal=(SELECT ordinal FROM "
                                      "s_generations WHERE gen_id=?) AND rel='legacy_fts'",
                                      (normal['gen_id'],)).fetchone()[0], 0)
        pin = store.pin_view(handle)
        try:
            self.assertIn('concepts/heron-route.md', [r['path'] for r in store.search(pin, 'heron', 20)])
        finally:
            store.release(pin)
        back = store.rollback_generation(handle, expected_head=normal)
        self.assertEqual(back['epoch'], normal['epoch'] + 1)
        self.assertEqual(self.recall(store, handle), before)
        while any(store.collect_view_rows(handle, limit=64).get(t) for t in ('s_cards', 's_legacy_fts', 'stage')):
            pass
        self.assertEqual(self.recall(store, handle), before)
        again = self.publish_normal(store, handle, back, manifest)
        self.assertEqual(again['epoch'], back['epoch'] + 1)

    def test_legacy_base_under_a_normal_head_converts_and_rolls_back_to_the_legacy_recall(self):
        self.publish()
        with self.writer() as view:
            handle = view.capture_view(self.worktree_id)
            legacy = self.recall(view, handle)
            head = view.head(handle)
            members = [dict(path=m['path'], blob_sha=m['blob_sha'], display_title=m['display_title'],
                            trust_digest=m['trust_digest'])
                       for m in view._db.execute('SELECT * FROM wiki_members WHERE gen_id=?', (head['gen_id'],))]
            manifest = {'scan_id': 'b' * 32, 'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                        'complete': True, 'sources': [], 'members': members}
            normal = self.publish_normal(view, handle, head, manifest)
            view.convert_to_row_format()
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        fresh = store.capture_view(self.worktree_id)
        self.assertEqual(store.head(fresh)['gen_id'], normal['gen_id'])
        back = store.rollback_generation(fresh, expected_head=store.head(fresh))
        self.assertEqual(self.recall(store, fresh), legacy)
        self.assertNotEqual(back['gen_id'], head['gen_id'])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(LegacyConvertChecks):
        if name in LegacyConvertChecks.__dict__:
            suite.addTest(LegacyConvertChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
