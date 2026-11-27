"""1.0.x-3 publish delta over a head: only the changed cards are proven again.

A publication over a vector-sealed head re-reads only the sets new to the spec,
builds FTS rows only for changed cards and reuses this process's prepare read
of the wiki files while their nodes match. Every case checks that the sealed
generation equals a full recomputation over the same rows (the reader's own
``pin_view`` proof included) and counts the reads it skipped.
"""
from contextlib import closing
import os
import sqlite3
from unittest import mock

from memvane.store.blob.blob_values import canonical
from memvane.store.tests.support import VectorCase
from memvane.store.view import view_fts, view_rows, view_store


class DeltaChecks(VectorCase):
    def changed(self, store, h, manifest, r, p, path='cards/01.md', body=b'# Card 1\nhello changed member\n'):
        """Rewrite one member's card and its set; ``(manifest, new blob)``."""
        (self.root / '.memvane/wiki' / path).write_bytes(body)
        blob = store.ingest_card(h.worktree_id, path)
        old = next(m for m in manifest['members'] if m['path'] == path)
        att = store.put_attestation(dict(
            blob_sha=blob, verification_kind='compiled', engine_identity='synthetic:codex',
            policy_digest=h.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1', verdict='verified', result_digest='e' * 64,
                         prior_trust=None),
            sources=[dict(fingerprint_id=manifest['sources'][0]['fingerprint_id'], spans=[dict(start=0, end=4)])]))
        self.put_vectors(store, blob, r, p, [[1.0, 0.0]])
        members = [dict(m, blob_sha=blob, trust_digest=att) if m is old else m for m in manifest['members']]
        return dict(manifest, members=members), blob

    def assert_full_proof(self, store, h, gen):
        """The sealed FTS and member values are what a full recomputation over the head computes."""
        row = dict(store._db.execute('SELECT * FROM s_generations WHERE gen_id=?', (gen,)).fetchone())
        cards = store._rows_open(store._db, row['view_id'])
        rows = store._rows_fts_rows(cards, cache={})
        _, physical = view_fts.head_proof(store._db, row['view_id'], rows, logical=False)
        text_acc = view_rows.fts_acc(rows)
        members = view_rows.member_acc(cards, store._rows_open_sources(store._db, row['view_id']),
                                       store._rows_open_card_sources(store._db, row['view_id']))
        self.assertEqual((row['fts_acc'], row['logical_fts_digest'], row['physical_fts_digest'], row['member_acc']),
                         (canonical(text_acc), view_rows.acc_digest('fts', text_acc), physical, canonical(members)))
        self.assertEqual(row['seal_digest'], view_rows.seal_digest(view_rows._seal_fields(store._incarnation, row)))
        store.release(store.pin_view(h))  # the reader's full proof of the head

    def test_head_publication_proves_only_the_changed_card(self):
        store, _, h, manifest, r, p, _, pin = self.ready(3)
        store.release(pin)
        manifest, blob = self.changed(store, h, manifest, r, p)
        with mock.patch.object(store, '_vectors', wraps=store._vectors) as sets, \
                mock.patch.object(store, '_rows_fts_rows', wraps=store._rows_fts_rows) as fts_rows, \
                mock.patch.object(view_store, '_regular', wraps=view_store._regular) as reads:
            base = store.head(h)
            gen = store.prepare_generation(h, manifest, expected_head=base, vector_spec=dict(recipe_id=r, profile_id=p))
            prepared_reads = reads.call_count
            store.publish_generation(h, gen, expected_head=base)
        # One set re-read (the new blob), one FTS row per changed side, no second wiki read.
        self.assertEqual([call.args[0] for call in sets.call_args_list], [blob])
        self.assertEqual(sorted(len(call.args[0]) for call in fts_rows.call_args_list), [1, 1])
        self.assertEqual((prepared_reads, reads.call_count), (3, 3))
        self.assertEqual(tuple(store._db.execute('SELECT coverage,recipe_id,profile_id FROM s_generations '
                                                 'WHERE gen_id=?', (gen,)).fetchone()), ('complete', r, p))
        self.assert_full_proof(store, h, gen)

    def test_lexical_head_publication_proves_every_set(self):
        store, _, h, manifest, r, p, _, pin = self.ready(3, lexical=True)
        store.release(pin)
        self.fill(store, h, manifest, r, p)
        with mock.patch.object(store, '_vectors', wraps=store._vectors) as sets:
            gen, pin = self.publish(store, h, manifest, r, p)
        store.release(pin)
        self.assertEqual(sets.call_count, 3)
        self.assert_full_proof(store, h, gen)

    def test_changed_file_node_rereads_the_wiki_before_publish(self):
        store, _, h, manifest, r, p, _, pin = self.ready(3)
        store.release(pin)
        manifest, _blob = self.changed(store, h, manifest, r, p)
        base = store.head(h)
        gen = store.prepare_generation(h, manifest, expected_head=base, vector_spec=dict(recipe_id=r, profile_id=p))
        card = self.root / '.memvane/wiki/cards/02.md'
        os.utime(card, ns=(card.stat().st_atime_ns, card.stat().st_mtime_ns + 1_000_000_000))
        with mock.patch.object(view_store, '_regular', wraps=view_store._regular) as reads:
            store.publish_generation(h, gen, expected_head=base)
        self.assertEqual(reads.call_count, 3)
        self.assert_full_proof(store, h, gen)

    def test_another_process_prepare_is_reread(self):
        store, _, h, manifest, r, p, _, pin = self.ready(3)
        store.release(pin)
        manifest, _blob = self.changed(store, h, manifest, r, p)
        base = store.head(h)
        gen = store.prepare_generation(h, manifest, expected_head=base, vector_spec=dict(recipe_id=r, profile_id=p))
        store._prepared_wiki = None  # the publish of another process (or store object) holds no prepare read
        with mock.patch.object(view_store, '_regular', wraps=view_store._regular) as reads:
            store.publish_generation(h, gen, expected_head=base)
        self.assertEqual(reads.call_count, 3)
        self.assert_full_proof(store, h, gen)

    def test_damaged_head_fts_refuses_the_delta_publication(self):
        store, _, h, manifest, r, p, gen0, pin = self.ready(3)
        store.release(pin)
        manifest, _blob = self.changed(store, h, manifest, r, p)
        view_id = store._db.execute('SELECT view_id FROM s_generations WHERE gen_id=?', (gen0,)).fetchone()[0]
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(f"UPDATE {view_fts.head_table(view_id)} SET body='forged' WHERE path_key='cards/02.md'")
        base = store.head(h)
        gen = store.prepare_generation(h, manifest, expected_head=base, vector_spec=dict(recipe_id=r, profile_id=p))
        error = self.bounded_refusal(store.publish_generation, h, gen, expected_head=base)
        self.assertEqual(error.reason, 'fts_rows_mismatch')
        self.assertEqual(store.head(h), base)
