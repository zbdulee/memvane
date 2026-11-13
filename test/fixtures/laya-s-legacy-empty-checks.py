#!/usr/bin/env python3
"""A zero-card legacy generation converts, takes an ordinary publication and rolls back as legacy."""
from contextlib import contextmanager
import importlib
import importlib.util
from pathlib import Path
import time
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('schema3_journal_fixture', HERE / 'laya-journal-schema3-checks.py')
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)


class EmptyLegacyChecks(journal.Schema3EndpointChecks):
    def setUp(self):
        # The seeded v6 install publishes an empty wiki: the shadow import is a zero-card legacy head.
        cards = journal.seed_module.base.published.CARDS
        for patch in (mock.patch.dict(cards, clear=True),
                      mock.patch.object(journal.seed_module.MoveSealChecks, 'PUBLISH_SIDECAR', False)):
            patch.start()
            self.addCleanup(patch.stop)
        super().setUp()

    def recall(self, store, handle):
        pin = store.pin_view(handle)
        try:
            kind = store._db.execute('SELECT kind,sidecar_present FROM s_generations WHERE gen_id=?',
                                     (pin.gen_id,)).fetchone()
            return tuple(kind), store.search(pin, 'heron', 10), store.card_locations(pin, ['concepts/x.md'])
        finally:
            store.release(pin)

    def test_zero_card_legacy_generation_round_trips_through_an_ordinary_publication(self):
        self.publish()
        with self.writer() as view:
            members = view._db.execute('SELECT COUNT(*) FROM wiki_members WHERE gen_id=?', (self.gens[0],)).fetchone()
            self.assertEqual(members[0], 0)
            view.convert_to_row_format()
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        handle = store.capture_view(self.worktree_id)
        legacy_head = store.head(handle)
        self.assertEqual(self.recall(store, handle), (('legacy', 0), [], {}))
        manifest = {'scan_id': 'c' * 32, 'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                    'complete': True, 'sources': [], 'members': []}
        gen = store.prepare_generation(handle, manifest, expected_head=legacy_head)
        normal = store.publish_generation(handle, gen, expected_head=legacy_head)
        self.assertEqual(self.recall(store, handle), (('normal', 0), [], {}))
        back = store.rollback_generation(handle, expected_head=normal)
        self.assertEqual(self.recall(store, handle), (('rollback', 0), [], {}))
        db = store._db
        rows = {r[0]: tuple(r[1:]) for r in db.execute('SELECT gen_id,member_acc,fts_acc FROM s_generations')}
        self.assertEqual(rows[back['gen_id']], rows[legacy_head['gen_id']],
                         'the rollback reproduces the zero-card legacy accumulators')
        while any(store.collect_view_rows(handle, limit=64).get(t) for t in ('s_cards', 'stage')):
            pass
        again = store.publish_generation(handle, store.prepare_generation(handle, manifest, expected_head=back),
                                         expected_head=back)
        self.assertEqual(again['epoch'], back['epoch'] + 1)


    def formats(self):
        """``(name, store path, worktree id)``: the seeded generation-format store and a row-format one."""
        checkout = importlib.import_module('laya_checkout_view')
        rows_path = self.case.state / 'schema3-rows-candidate.sqlite'
        worktree = checkout.register_worktree(str(self.case.project), self.repository)
        self.views.ViewStore.create(rows_path, project_root=str(self.case.project), repository=self.repository,
                                    worktree=worktree, scope_id=self.case.route['scopeId'],
                                    row_generations=True).close()
        return (('generation', self.store_path, self.worktree_id), ('row', rows_path, worktree['worktreeId']))

    @contextmanager
    def legacy_stage(self, path, worktree_id):
        """A prepared zero-card legacy import: ``(store, handle, base head, gen id, proof)``."""
        scan = {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}
        self.store_path, self.worktree_id = path, worktree_id
        with self.seed.authority(), self.writer() as view:
            decision = journal.J.select_route(str(self.case.project), action='recall')
            with self.lease.resolve_endpoint(self.case.project, action='recall', host_root=self.case.plugin) as endpoint:
                with self.opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    self.migration.import_wiki_batch(view, worktree_id=worktree_id, snapshot=snapshot,
                        endpoint=endpoint, decision=decision, after=None, provenance_scan=scan,
                        deadline=time.monotonic() + 10)
                    handle = view.capture_view(worktree_id)
                    proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                             'provenance_scan': scan}
                    evidence = self.migration.verify_generation_members(view, worktree_id=worktree_id,
                        **proof, deadline=time.monotonic() + 30)
                    self.assertEqual(evidence['members'], [])
                    manifest = {'scan_id': scan['id'], 'policy_digest': handle.policy_digest,
                                'revocation_epoch': 0, 'complete': True, 'sources': [], 'members': []}
                    head = view.head(handle)
                    gen = view.prepare_generation(handle, manifest, expected_head=head, legacy_snapshot=proof)
                    yield view, handle, head, gen, proof

    def test_zero_card_legacy_stage_refuses_an_ordinary_publication_like_the_generation_format(self):
        for name, path, worktree_id in self.formats():
            with self.subTest(format=name), self.legacy_stage(path, worktree_id) as (view, handle, head, gen, _proof):
                self.refused('migration_publish_proof_required', view.publish_generation, handle, gen,
                             expected_head=head)
                self.assertEqual(view.head(handle), head)

    def test_replayed_legacy_publication_proves_the_fresh_snapshot_like_the_generation_format(self):
        for name, path, worktree_id in self.formats():
            with self.subTest(format=name), self.legacy_stage(path, worktree_id) as (view, handle, head, gen, proof):
                published = view.publish_generation(handle, gen, expected_head=head, legacy_snapshot=proof)
                other = dict(proof, provenance_scan={'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()})
                self.refused('migration_snapshot_changed', view.publish_generation, handle, gen,
                             expected_head=head, legacy_snapshot=other)
                self.refused('migration_source_revision_unproved', view.publish_generation, handle, gen,
                             expected_head=head, legacy_snapshot=dict(proof, source_revision='0' * 64))
                self.assertEqual(view.publish_generation(handle, gen, expected_head=head, legacy_snapshot=proof),
                                 published)
                self.assertEqual(view.head(handle), published)


    def test_replayed_legacy_publication_after_a_new_commit_refuses_like_the_generation_format(self):
        """A crash after the commit and before the receipt, then a commit on the same branch: the
        replay with a fresh handle no longer names the published checkout."""
        import os
        import subprocess
        git = ['git', '-C', str(self.case.project), '-c', 'commit.gpgsign=false', '-c',
               'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
        for name, path, worktree_id in self.formats():
            with self.subTest(format=name), self.legacy_stage(path, worktree_id) as (view, handle, head, gen, proof):
                published = view.publish_generation(handle, gen, expected_head=head, legacy_snapshot=proof)
                subprocess.run(git + ['commit', '--allow-empty', '-qm', 'after ' + name],
                               check=True, capture_output=True, timeout=10)
                time.sleep(1.1)
                moved = view.capture_view(worktree_id)
                self.assertNotEqual(moved.head_oid, handle.head_oid)
                self.refused('generation_binding_mismatch', view.publish_generation, moved, gen,
                             expected_head=head, legacy_snapshot=proof)
                self.assertEqual(view.head(moved), published)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(EmptyLegacyChecks):
        if name in EmptyLegacyChecks.__dict__:
            suite.addTest(EmptyLegacyChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
