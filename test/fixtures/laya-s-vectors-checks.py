#!/usr/bin/env python3
"""Row-format vector coverage: head-bound fill cursor, complete generations, exact search."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('s_fill_fixture', HERE / 'laya-view-fill-checks.py')
fill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fill)


class RowVectorChecks(fill.FillCase):
    def rows_configured(self):
        repository, worktree = self.bind()
        store = self.Store.create(str(self.db), project_root=str(self.root), repository=repository,
                                  worktree=worktree, scope_id=self.scope, row_generations=True)
        self.track(store)
        handle = store.capture_view(worktree['worktreeId'])
        manifest = self.snapshot(store, handle)
        recipe_id = store.register_recipe(fill.sets.recipe())
        profile_id = store.register_profile(fill.sets.profile())
        return store, repository, handle, manifest, recipe_id, profile_id

    def publish(self, store, handle, manifest, vector_spec=None):
        head = store.head(handle)
        gen = store.prepare_generation(handle, manifest, expected_head=head, vector_spec=vector_spec)
        return store.publish_generation(handle, gen, expected_head=head)

    def test_fill_cursor_binds_the_head_and_spec_without_target_rows(self):
        store, _, handle, manifest, r, p = self.rows_configured()
        self.publish(store, handle, manifest)
        first = store.fill_status(handle, manifest, r, p)
        self.assert_status(first, 0, False)
        self.assertEqual(store.fill_status(handle, manifest, r, p)['job_id'], first['job_id'])
        done = self.fill(store, handle, manifest, r, p)
        self.assertEqual((done['status'], done['complete'], done['job_id']), ('complete', True, first['job_id']))
        self.assert_status(store.fill_status(handle, manifest, r, p), 1, True)
        again = self.fill(store, handle, manifest, r, p)
        self.assertEqual((again['status'], again['filled']), ('complete', 0))
        db = store._db
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_fill_cursors').fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM fill_targets').fetchone()[0], 0)

    def test_completion_rechecks_every_set_and_restarts_when_one_is_missing(self):
        import sqlite3
        store, repository, handle, manifest, r, p = self.rows_configured()
        self.publish(store, handle, manifest)
        self.fill(store, handle, manifest, r, p)
        self.assert_status(store.fill_status(handle, manifest, r, p), 1, True)
        store.close()
        db = sqlite3.connect(str(self.db))
        triggers = [row for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('vectors','embedding_sets')")]
        for name, _ in triggers:
            db.execute(f'DROP TRIGGER {name}')
        db.execute('DELETE FROM vectors'); db.execute('DELETE FROM embedding_sets')
        for _, sql in triggers:
            db.execute(sql)
        db.commit(); db.close()
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        self.assert_status(again.fill_status(fresh, manifest, r, p), 0, False)
        before = len(self.calls['embedder'])
        done = self.fill(again, fresh, manifest, r, p)
        self.assertEqual((done['status'], done['complete'], done['filled']), ('complete', True, 1))
        self.assertGreater(len(self.calls['embedder']), before)
        self.assert_status(again.fill_status(fresh, manifest, r, p), 1, True)

    def test_complete_generation_publishes_and_answers_exact_and_hybrid_search(self):
        store, repository, handle, manifest, r, p = self.rows_configured()
        self.publish(store, handle, manifest)
        self.fill(store, handle, manifest, r, p)
        head = self.publish(store, handle, manifest, dict(recipe_id=r, profile_id=p))
        row = store._db.execute('SELECT coverage,recipe_id,profile_id FROM s_generations WHERE gen_id=?',
                                (head['gen_id'],)).fetchone()
        self.assertEqual(tuple(row), ('complete', r, p))
        store.close()
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        pin = again.pin_view(fresh)
        try:
            exact = again.search_exact(pin, [1.0, 0.0], 10, profile_id=p)
            self.assertEqual([hit['path'] for hit in exact], ['cards/a.md'])
            hybrid = again.search_hybrid(pin, 'hello', [1.0, 0.0], 10, profile_id=p)
            self.assertEqual(hybrid['semantic_status'], 'complete')
            self.assertEqual([hit['path'] for hit in hybrid['semantic']], ['cards/a.md'])
        finally:
            again.release(pin)

    def test_complete_generation_refuses_a_missing_set_before_any_write(self):
        store, _, handle, manifest, r, p = self.rows_configured()
        head = self.publish(store, handle, manifest)
        jobs = store._db.execute('SELECT COUNT(*) FROM s_generations').fetchone()[0]
        reason = self.refused(store.prepare_generation, handle, manifest, expected_head=head,
                              vector_spec=dict(recipe_id=r, profile_id=p)).reason
        self.assertEqual(reason, 'vector_coverage_incomplete')
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_generations').fetchone()[0], jobs)

    def test_a_new_head_resets_the_cursor_and_rollback_restores_complete_coverage(self):
        store, _, handle, manifest, r, p = self.rows_configured()
        self.publish(store, handle, manifest)
        self.fill(store, handle, manifest, r, p)
        complete = self.publish(store, handle, manifest, dict(recipe_id=r, profile_id=p))
        changed = self.snapshot(store, handle, body=b'# Changed\nhello changed card\n')
        lexical = self.publish(store, handle, changed)
        self.assert_status(store.fill_status(handle, changed, r, p), 0, False)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_fill_cursors').fetchone()[0], 1)
        back = store.rollback_generation(handle, expected_head=lexical)
        row = store._db.execute('SELECT coverage,recipe_id,profile_id FROM s_generations WHERE gen_id=?',
                                (back['gen_id'],)).fetchone()
        self.assertEqual(tuple(row), ('complete', r, p))
        pin = store.pin_view(handle)
        try:
            self.assertEqual([hit['path'] for hit in store.search_exact(pin, [1.0, 0.0], 10, profile_id=p)],
                             ['cards/a.md'])
        finally:
            store.release(pin)
        self.assertNotEqual(back['gen_id'], complete['gen_id'])

    def test_generation_format_vector_generation_converts_with_the_same_exact_search(self):
        store, repository, handle, manifest, r, p = self.configured()
        self.fill(store, handle, manifest, r, p)
        self.publish(store, handle, manifest, dict(recipe_id=r, profile_id=p))
        pin = store.pin_view(handle)
        before = [(h['path'], h['score']) for h in store.search_exact(pin, [1.0, 0.0], 10, profile_id=p)]
        store.release(pin)
        store.convert_to_row_format()
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        pin = again.pin_view(fresh)
        try:
            after = [(h['path'], h['score']) for h in again.search_exact(pin, [1.0, 0.0], 10, profile_id=p)]
        finally:
            again.release(pin)
        self.assertEqual(after, before)
        self.assertTrue(before)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(RowVectorChecks):
        if name in RowVectorChecks.__dict__:
            suite.addTest(RowVectorChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
