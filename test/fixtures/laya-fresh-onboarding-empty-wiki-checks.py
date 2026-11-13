#!/usr/bin/env python3
"""New-project onboarding with an empty wiki, end to end.

Same installer path and stubs as ``laya-fresh-onboarding-e2e-checks.py``,
but the wiki holds no card: the initial build must still seal a zero-member
generation, finalize, activate and serve recall with an empty result.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import runpy
import shutil
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
E2E = runpy.run_path(str(HERE / 'laya-fresh-onboarding-e2e-checks.py'))

from memvane.settings import config  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_host_response as response  # noqa: E402
from memvane.route import index_host_route as host_route  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
import laya_incremental_docs as incremental  # noqa: E402
from laya_index_runtime import LayaIndexRuntime  # noqa: E402
import laya_product_install as product  # noqa: E402
import laya_product_sync_worker as worker  # noqa: E402

BUILD = E2E['BUILD']


class EmptyWikiOnboarding(E2E['FreshOnboarding']):
    # Only the empty-wiki cases below run here.
    test_minimal_settings_project_is_built_selected_and_recalled = None
    test_qmd_origin_marker_is_refused_with_a_clear_reason = None

    def setUp(self):
        super().setUp()
        # A build that cannot seal spins through whole wakes; keep a failure short.
        patcher = mock.patch.object(worker, 'BUILD_WAKE_SECONDS', 5)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The base cards wait outside the wiki; a case may bring one back.
        self.aside = self.home / 'aside'
        shutil.move(str(self.wiki / 'concepts'), str(self.aside))
        (self.wiki / 'concepts').mkdir()

    def head_members(self):
        """Paths of the candidate head generation, in the build's own scope."""
        identity = self.record()['identity']
        with closing(LayaIndexA(identity['candidateDb'])) as index:
            row = index.db.execute('SELECT gen FROM head WHERE scope=?',
                                   (identity['scopeId'],)).fetchone()
            self.assertIsNotNone(row)
            # The head carries a seal under the build profile, even with no row.
            seal = index.db.execute('SELECT profile_id FROM gen_vector_seals WHERE gen=?',
                                    (row[0],)).fetchone()
            self.assertEqual(seal, (identity['vectorProfileId'],))
            return [path for (path,) in index.db.execute(
                'SELECT path FROM membership WHERE gen=? ORDER BY path', (row[0],))]

    def prepare(self):
        plan = product.plan_fresh_wiki_origin(self.project)
        self.assertEqual(plan['status'], 'review_required', plan)
        prepared = product.prepare_fresh_wiki_origin(
            project_root=self.project, source_root=str(self.source),
            plugin_root=str(self.plugin), state_root=str(self.state),
            commit_id=E2E['COMMIT'], expected_proposal_sha256=plan['proposalSha256'],
            install_id=BUILD['INSTALL'],
            accepted_provider_sha256=self.review['proposalSha256'],
            model_path='/models/m.gguf', dimension=2)
        self.assertEqual(prepared['status'], 'initial_build_registered', prepared)
        return (self.state / 'reviewed-route.json').read_bytes()

    def settle_sealed(self):
        settled = self.until_settled(limit=8)
        self.assertEqual(settled[-1]['initialBuild'], 'sealed', settled[-3:])

    def bind_and_activate(self, route_bytes):
        bound = self.finalize()
        self.assertEqual(bound['status'], 'cutover_prepared', bound)
        self.assertEqual(self.record()['phase'], 'bound')
        self.assertEqual(self.head_members(), [])
        with product._install_lock(self.state):
            active = product.activate_origin_selection(self.project, self.journal_path,
                                                       route_bytes)
        self.assertEqual(active['phase'], 'active', active)
        self.assertEqual((self.auto / fresh.ROUTE).read_bytes(), route_bytes)
        decision = host_route.select_route(str(self.project), action='recall')
        self.assertEqual(decision['state'], 'candidate', decision)
        return decision

    def recall(self, decision, prompt):
        proof = journal_mod.verify_selected_route_read_only(decision)
        self.assertTrue(proof['readOnlyAllowed'])
        with mock.patch('laya_local_embed_provider.ReviewedLocalQmdProvider',
                        E2E['QueryProvider']), \
                mock.patch('laya_embed_resident.admit_query', lambda **_kw: None):
            return response._serve_recall('claude', decision, {'prompt': prompt}, proof,
                                          journal_mod.verify_selected_route_read_only)

    def assert_empty_recall(self, decision):
        served = self.recall(decision, 'emerald heron')
        self.assertIsInstance(served, dict)
        context = (served.get('hookSpecificOutput') or {}).get('additionalContext', '')
        self.assertNotIn('concepts/', context)
        self.assertNotIn('heron nests', context)
        self.assertNotIn('error', json.dumps(served).lower())
        return served

    def test_empty_wiki_project_seals_activates_and_recalls_nothing(self):
        self.assertEqual(list(self.wiki.rglob('*.md')), [])
        route_bytes = self.prepare()
        self.settle_sealed()
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.settle_sealed()
        self.assert_empty_recall(self.bind_and_activate(route_bytes))

    def test_last_card_removed_publishes_an_empty_generation(self):
        # One card is built and sealed, then removed: the members drop to
        # zero and the rebuilt head is an empty, sealed generation.
        card = self.wiki / 'concepts' / 'heron.md'
        shutil.copy(str(self.aside / 'heron.md'), str(card))
        route_bytes = self.prepare()
        self.settle_sealed()
        self.assertEqual(len(self.head_members()), 1)
        card.unlink()
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.settle_sealed()
        decision = self.bind_and_activate(route_bytes)
        # The initial build publishes per-document sidecar rows but never
        # retires them; the selected sync does after an indexed/unchanged
        # step (_sync_capacity). Run that retire step, then recall is empty.
        identity = self.record()['identity']
        with LayaIndexRuntime(source_root=worker.wiki_index_roots(identity['collections']),
                              scope=identity['scopeId'], db_path=identity['candidateDb'],
                              queue_path=identity['queueDb'],
                              db_incarnation=identity['dbIncarnation'],
                              catalog_path=identity['catalogDb'],
                              binding_store_path=identity['bindingStore'],
                              wiki_only=True) as runtime:
            self.assertEqual(incremental.retire_missing_documents(
                runtime, identity, before_write=lambda: None), 1)
        self.assert_empty_recall(decision)

if __name__ == '__main__':
    unittest.main()
