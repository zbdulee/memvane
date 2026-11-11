"""Synthetic v6 card with a one-character title and a searchable filename."""
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
spec = importlib.util.spec_from_file_location('empty_query_seed', HERE / 'laya-reader-move-seal-checks.py')
seed_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_module)


class EmptyQueryChecks(unittest.TestCase):
    def _run_case(self, mode):
        published = seed_module.base.published
        original_card = published.card

        def card(title, body):
            result = original_card(title, body)
            return result.replace('## Summary', '# C') if title in ('Heron route', 'C') else result

        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False

        seed = BaseOnly('runTest')
        cards = dict(published.CARDS)
        if mode == 'no_query':
            del cards['concepts/heron-route.md']
            cards['concepts/a.md'] = ('C', 'x')
        with mock.patch.object(published, 'card', new=card), \
             mock.patch.object(published, 'CARDS', cards):
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
        migration = importlib.import_module('laya_shadow_migration')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = views.ViewStore.create(case.state / 'empty-query-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)
        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 2):
            migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            for _ in range(40):
                result = migration.advance_shadow(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 60)
                if result['migration_state'] == 'caught_up':
                    break
            self.assertEqual(result['migration_state'], 'caught_up')
            generation = result['intent']['published']['generation']
            rows = store._db.execute('SELECT path,display_title FROM wiki_members WHERE gen_id=? '
                                     'AND eligible=1 ORDER BY path', (generation,)).fetchall()
            expected_path = 'concepts/a.md' if mode == 'no_query' else 'concepts/heron-route.md'
            self.assertIn((expected_path, 'C'), [tuple(row) for row in rows])
            self.assertEqual(len(rows), 3)
            def step():
                return migration.advance_shadow(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 60)
            if mode == 'no_query':
                with mock.patch.object(store, 'search', wraps=store.search) as search:
                    with self.assertRaisesRegex(Exception, 'migration_recall_parity_mismatch'):
                        step()
                self.assertEqual(search.call_count, 0)
                return
            if mode in ('search_error', 'no_rows'):
                # Candidate search must not try another prompt after real search failure.
                effect = RuntimeError('candidate_search_failure') if mode == 'search_error' else None
                kwargs = {'side_effect': effect} if effect else {'return_value': []}
                with mock.patch.object(store, 'search', **kwargs) as search:
                    reason = 'candidate_search_failure' if effect else 'migration_recall_parity_mismatch'
                    with self.assertRaisesRegex(Exception, reason):
                        step()
                self.assertEqual(search.call_count, 1)
                return
            intent = result['intent']
            record = migration._initial_parity(intent)
            first = migration._candidate_sample(store, intent, record, time.monotonic() + 60)
            second = migration._candidate_sample(store, intent, record, time.monotonic() + 60)
            self.assertEqual(first, second)
            for _ in range(20):
                result = step()
                if result['migration_state'] == 'shadow_verified':
                    break
            self.assertEqual(result['migration_state'], 'shadow_verified')
            self.assertEqual(result['intent']['parity']['members'], 3)
            self.assertIsNotNone(result['intent']['parity']['seal'])

    def test_one_character_title_uses_searchable_card_prompt(self):
        self._run_case('normal')

    def test_no_searchable_prompt_fails_closed(self):
        self._run_case('no_query')

    def test_search_error_is_not_skipped(self):
        self._run_case('search_error')

    def test_empty_search_result_is_not_skipped(self):
        self._run_case('no_rows')


if __name__ == '__main__':
    print('child-python=%s version=%s' % (sys.executable, sys.version.split()[0]), flush=True)
    unittest.main(verbosity=2)
