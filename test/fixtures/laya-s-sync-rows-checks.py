#!/usr/bin/env python3
"""The schema3 sync pass publishes row-format (4) view stores like generation-format (3) ones.

Every U12c check of ``laya-schema3-sync-publish-checks.py`` runs again with
the seed's view store created in the row format: the same actual
``laya_product_sync_worker.run`` pass, persisted schema3 recall and journal
images. Only the checks' direct store reads change to the row relations.
No model or provider starts.
"""
from contextlib import closing
import importlib
import importlib.util
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_sync_publish', ROOT / 'test/fixtures/laya-schema3-sync-publish-checks.py')
publish = importlib.util.module_from_spec(spec); spec.loader.exec_module(publish)


class RowStoreReads:
    """Seed the view store in the row format; the checks' direct store reads use the row relations."""

    def setUp(self):
        views = importlib.import_module('laya_view_store')
        original = views.ViewStore.create.__func__

        def create(cls, *args, **kwargs):
            kwargs.setdefault('row_generations', True)
            return original(cls, *args, **kwargs)
        patcher = mock.patch.object(views.ViewStore, 'create', classmethod(create))
        patcher.start(); self.addCleanup(patcher.stop)
        super().setUp()
        with self.ro() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0], 4)

    def ro(self):
        return closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True))

    @staticmethod
    def at(db, gen_id):
        row = db.execute('SELECT view_id,ordinal FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
        assert row is not None, gen_id
        return row[0], row[1]

    # -- the parent's direct store reads, on the row relations ------------------
    def head(self):
        with self.ro() as db:
            rows = db.execute('SELECT g.gen_id,h.epoch FROM s_heads h JOIN s_generations g '
                              'ON g.view_id=h.view_id AND g.ordinal=h.ordinal').fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def view_heads(self):
        with self.ro() as db:
            return sorted(db.execute('SELECT h.view_id,g.gen_id,h.epoch FROM s_heads h JOIN s_generations g '
                                     'ON g.view_id=h.view_id AND g.ordinal=h.ordinal').fetchall())

    def members(self, gen_id):
        with self.ro() as db:
            view_id, ordinal = self.at(db, gen_id)
            return {path: (blob, eligible) for path, blob, eligible in db.execute(
                'SELECT path,blob_sha,eligible FROM s_cards WHERE view_id=? AND gen_from<=? '
                'AND (gen_to IS NULL OR gen_to>?)', (view_id, ordinal, ordinal))}

    def card_sources(self, gen_id, path):
        with self.ro() as db:
            view_id, ordinal = self.at(db, gen_id)
            return db.execute('SELECT count(*) FROM s_card_sources WHERE view_id=? AND card_path=? '
                              'AND gen_from<=? AND (gen_to IS NULL OR gen_to>?)',
                              (view_id, path, ordinal, ordinal)).fetchone()[0]

    def generation_sources(self, gen_id):
        """``{path: (presence, observed path)}`` of one generation's source rows."""
        with self.ro() as db:
            view_id, ordinal = self.at(db, gen_id)
            return {path: ('present', observed) for path, observed in db.execute(
                'SELECT s.path,o.path FROM s_sources s JOIN s_source_observations o '
                'ON o.observation_id=s.observation_id WHERE s.view_id=? AND s.gen_from<=? '
                'AND (s.gen_to IS NULL OR s.gen_to>?)', (view_id, ordinal, ordinal))}

    def attested_source_paths(self, gen_id, card):
        store = self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                                 scope_id=self.case.route['scopeId'])
        try:
            with self.ro() as db:
                view_id, ordinal = self.at(db, gen_id)
                trust = db.execute('SELECT trust_digest FROM s_cards WHERE view_id=? AND path=? AND gen_from<=? '
                                   'AND (gen_to IS NULL OR gen_to>?)', (view_id, card, ordinal, ordinal)).fetchone()[0]
            self.assertIsNotNone(trust)
            evidence = store.attestation(trust)
            return sorted(store.fingerprint(item['fingerprint_id'])['path'] for item in evidence['sources'])
        finally:
            store.close()

    def member_trust(self, gen_id, card):
        with self.ro() as db:
            view_id, ordinal = self.at(db, gen_id)
            return db.execute('SELECT trust_digest,eligible FROM s_cards WHERE view_id=? AND path=? AND gen_from<=? '
                              'AND (gen_to IS NULL OR gen_to>?)', (view_id, card, ordinal, ordinal)).fetchone()


class RowSyncPublishChecks(RowStoreReads, publish.Schema3SyncPublishChecks):
    pass


# Only the U12c checks run here, each on a row-format store.
for _name in dir(publish.seed_module.Schema3WriterPlanChecks):
    if _name.startswith('test') and _name not in vars(publish.Schema3SyncPublishChecks):
        setattr(RowSyncPublishChecks, _name, None)


if __name__ == '__main__':
    unittest.main()
