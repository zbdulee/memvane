#!/usr/bin/env python3
"""Model-free tests for the source-only QMD fork search boundary."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import qmd_fork_scope_prototype as fts
import qmd_fork_source_adapter as adapter
import worktree_query_slice as candidate


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ForkScopePrototype(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        candidate.create_schema(self.db)
        fts.create_fts(self.db)

    def stage(self, worktree, generation, pages):
        candidate.stage(self.db, worktree, generation,
                        {path: digest(body) for path, body in pages.items()},
                        {digest(body): (body, 1.0) for body in pages.values()})
        fts.index_staged_content(self.db)

    def test_fts_global_top_k_misses_target_but_scoped_finds_it(self):
        self.stage('a', 'g1', {'same.md': 'target'})
        self.stage('b', 'g1', {'same.md': 'target target target decoy',
                              **{f'decoy-{i}.md': f'target target target decoy {i}'
                                 for i in range(201)}})
        self.assertTrue(candidate.publish(self.db, 'a', None, 'g1'))
        token = candidate.pin_request(self.db, 'a')
        self.assertEqual(fts.legacy_global_lex(self.db, token, 'target', 1), [])
        self.assertEqual(fts.scoped_lex(self.db, token, 'target', 1)[0][0], 'same.md')
        self.assertEqual(fts.scoped_lex(self.db, token, 'target', 1)[0][1],
                         digest('target'))

    def test_identical_hash_reused_and_old_pin_survives_publication(self):
        self.stage('a', 'g1', {'old.md': 'shared target'})
        self.stage('b', 'g1', {'other.md': 'shared target'})
        self.assertEqual(self.db.execute('SELECT count(*) FROM prototype_fts').fetchone()[0], 1)
        self.assertTrue(candidate.publish(self.db, 'a', None, 'g1'))
        old = candidate.pin_request(self.db, 'a')
        self.stage('a', 'g2', {'new.md': 'different target'})
        self.assertTrue(candidate.publish(self.db, 'a', 'g1', 'g2'))
        self.assertEqual([r[0] for r in fts.scoped_lex(self.db, old, 'target', 2)],
                         ['old.md'])
        current = candidate.pin_request(self.db, 'a')
        self.assertEqual([r[0] for r in fts.scoped_lex(self.db, current, 'target', 2)],
                         ['new.md'])
        candidate.release_request(self.db, old)
        with self.assertRaisesRegex(ValueError, 'pin_unavailable'):
            fts.scoped_lex(self.db, old, 'target', 1)

    def test_adapter_requires_capability_before_backend_read(self):
        self.stage('a', 'g1', {'x.md': 'target'})
        candidate.publish(self.db, 'a', None, 'g1')
        token = candidate.pin_request(self.db, 'a')

        class Unsupported:
            capabilities = adapter.ScopeCapabilities(True, False, True)

            def search(self, **_):
                raise AssertionError('backend must not be called')

        with self.assertRaisesRegex(ValueError, 'backend_scope_capability_missing'):
            adapter.search_phase(self.db, token, Unsupported(), 'vec', 'target', 1)

    def test_two_phases_keep_old_scope_after_cas_and_reject_foreign_hit(self):
        self.stage('a', 'g1', {'same.md': 'old target'})
        self.stage('b', 'g1', {'same.md': 'foreign target'})
        candidate.publish(self.db, 'a', None, 'g1')
        old = candidate.pin_request(self.db, 'a')
        self.stage('a', 'g2', {'same.md': 'new target'})
        self.assertTrue(candidate.publish(self.db, 'a', 'g1', 'g2'))

        class Fake:
            capabilities = adapter.ScopeCapabilities(True, True, True)

            def __init__(self):
                self.scopes = []

            def search(self, *, phase, query, limit, scope):
                self.scopes.append((phase, scope))
                return [adapter.SearchHit(scope.collection_id, 'same.md',
                                          digest('old target'), 1.0)]

        backend = Fake()
        lex, vec = adapter.search_both(self.db, old, backend, 'target', 'target', 1)
        self.assertEqual(len(lex), len(vec))
        self.assertEqual(backend.scopes[0][1], backend.scopes[1][1])
        self.assertEqual(backend.scopes[0][1].generation, 'g1')

        class Foreign(Fake):
            def search(self, *, phase, query, limit, scope):
                return [adapter.SearchHit(scope.collection_id, 'same.md',
                                          digest('foreign target'), 1.0)]

        with self.assertRaisesRegex(ValueError, 'backend_result_membership_mismatch'):
            adapter.search_phase(self.db, old, Foreign(), 'vec', 'target', 1)
        candidate.release_request(self.db, old)
        with self.assertRaisesRegex(ValueError, 'pin_unavailable'):
            adapter.search_phase(self.db, old, backend, 'lex', 'target', 1)


if __name__ == '__main__':
    unittest.main()
