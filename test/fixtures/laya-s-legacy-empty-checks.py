#!/usr/bin/env python3
"""A zero-card legacy generation converts, takes an ordinary publication and rolls back as legacy."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

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


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(EmptyLegacyChecks):
        if name in EmptyLegacyChecks.__dict__:
            suite.addTest(EmptyLegacyChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
