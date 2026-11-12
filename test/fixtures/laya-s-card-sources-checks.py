#!/usr/bin/env python3
"""Row-format (4) card source rows: a trusted card's source spans as view intervals.

The generation format (3) keeps them as per-generation ``card_sources`` rows;
``card_locations`` answers from them. The row format keeps them in
``s_card_sources`` with stage changes, rollback deltas, the member
accumulator, conversion and GC. Synthetic stores only.
"""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('s_publish_fixture', HERE / 'laya-s-incremental-publish-checks.py')
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


class CardSourceChecks(publish.IncrementalPublicationChecks):
    def line_card(self, store, handle, body, spans):
        """A manifest whose one trusted card cites ``notes/src.md`` with ``lines-v1`` spans."""
        manifest = self.snapshot(store, handle, body=body)
        fp = store.fingerprint(manifest['sources'][0]['fingerprint_id'])
        member = manifest['members'][0]
        member['trust_digest'] = store.put_attestation(dict(
            blob_sha=member['blob_sha'], verification_kind='compiled', engine_identity='synthetic:codex',
            policy_digest=handle.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v2', verdict='verified', result_digest='e' * 64,
                         prior_trust=None, verdict_digest='f' * 64, verified_mode='cross-engine',
                         verify_engine='claude'),
            sources=[dict(fingerprint_id=manifest['sources'][0]['fingerprint_id'],
                          spans=[dict(kind='lines-v1', path=fp['path'], sourceSha256=fp['source_sha'],
                                      startLine=start, endLine=end, spanSha256='d' * 64)
                                 for start, end in spans])]))
        return manifest

    def publish_manifest(self, store, handle, manifest):
        head = store.head(handle)
        gen = store.prepare_generation(handle, manifest, expected_head=head)
        return store.publish_generation(handle, gen, expected_head=head)

    def locations(self, store, handle):
        pin = store.pin_view(handle)
        try:
            return store.card_locations(pin, ['cards/a.md', 'cards/absent.md'])
        finally:
            store.release(pin)

    def card_source_rows(self, store, handle):
        return [tuple(row) for row in store._db.execute(
            'SELECT card_path,source_path,gen_from,gen_to FROM s_card_sources WHERE view_id=? '
            'ORDER BY gen_from', (handle.view_id,))]

    def test_01_compiled_line_spans_locate_on_a_row_head(self):
        store, _, handle = self.rows_store()
        self.publish_manifest(store, handle, self.line_card(store, handle, b'# Alpha\nhello compiled\n',
                                                            [(7, 7), (3, 5)]))
        self.assertEqual(self.locations(store, handle),
                         {'cards/a.md': ['notes/src.md#L3-L5', 'notes/src.md#L7-L7']})
        rows = self.card_source_rows(store, handle)
        self.assertEqual([(r[0], r[1], r[3]) for r in rows], [('cards/a.md', 'notes/src.md', None)])
        # The first generation records no rollback delta (no prior).
        self.assertEqual(store._db.execute("SELECT COUNT(*) FROM s_deltas WHERE rel='card_source'").fetchone()[0], 0)

    def test_02_byte_spans_still_refuse_like_the_generation_format(self):
        store, _, handle = self.rows_store()
        self.publish(store, handle, b'# Alpha\nhello byte spans\n')
        pin = store.pin_view(handle)
        try:
            self.assertEqual(self.refused(store.card_locations, pin, ['cards/a.md']).reason,
                             'legacy_card_trust_invalid')
        finally:
            store.release(pin)

    def test_03_new_trust_moves_card_sources_and_rollback_restores_them(self):
        store, _, handle = self.rows_store()
        body = b'# Alpha\nhello trust moves\n'
        first = self.publish_manifest(store, handle, self.line_card(store, handle, body, [(1, 2)]))
        second = self.publish_manifest(store, handle, self.line_card(store, handle, body, [(4, 6)]))
        self.assertEqual(self.locations(store, handle), {'cards/a.md': ['notes/src.md#L4-L6']})
        deltas = store._db.execute("SELECT COUNT(*) FROM s_deltas WHERE rel='card_source'").fetchone()[0]
        self.assertEqual(deltas, 1)
        rows = self.card_source_rows(store, handle)
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0][3])
        self.assertIsNone(rows[1][3])
        back = store.rollback_generation(handle, expected_head=second)
        self.assertEqual(back['epoch'], second['epoch'] + 1)
        self.assertEqual(self.locations(store, handle), {'cards/a.md': ['notes/src.md#L1-L2']})
        self.assertNotEqual(back['gen_id'], first['gen_id'])
        # An unchanged republish writes no card source rows.
        before = len(self.card_source_rows(store, handle))
        self.publish_manifest(store, handle, self.line_card(store, handle, body, [(1, 2)]))
        self.assertEqual(len(self.card_source_rows(store, handle)), before)

    def test_04_collect_removes_card_source_intervals_older_than_the_prior(self):
        store, _, handle = self.rows_store()
        body = b'# Alpha\nhello collected\n'
        for spans in ([(1, 1)], [(2, 2)], [(3, 3)], [(4, 4)]):
            self.publish_manifest(store, handle, self.line_card(store, handle, body, spans))
        removed = store.collect_view_rows(handle, limit=64)
        self.assertEqual(removed['s_card_sources'], 2)
        self.assertEqual(len(self.card_source_rows(store, handle)), 2)
        self.assertEqual(self.locations(store, handle), {'cards/a.md': ['notes/src.md#L4-L4']})

    def test_05_conversion_keeps_card_source_locations_and_their_delta(self):
        store, repository, handle = self.seeded()
        body = b'# Alpha\nhello converted\n'
        self.publish_manifest(store, handle, self.line_card(store, handle, body, [(2, 3)]))
        head = self.publish_manifest(store, handle, self.line_card(store, handle, body, [(5, 8)]))
        store.convert_to_row_format()
        again = self.reopen(repository)
        self.track(again)
        moved = again.capture_view(handle.worktree_id)
        self.assertEqual(self.locations(again, moved), {'cards/a.md': ['notes/src.md#L5-L8']})
        self.assertEqual(again._db.execute("SELECT COUNT(*) FROM s_deltas WHERE rel='card_source'").fetchone()[0], 1)
        back = again.rollback_generation(moved, expected_head=again.head(moved))
        self.assertEqual(back['epoch'], head['epoch'] + 1)
        self.assertEqual(self.locations(again, moved), {'cards/a.md': ['notes/src.md#L2-L3']})

    def test_06_one_source_path_merges_one_observation_and_refuses_two(self):
        store, _, handle = self.rows_store()
        manifest = self.snapshot(store, handle, body=b'# Alpha\nhello merged\n')
        fingerprint = manifest['sources'][0]['fingerprint_id']
        fp = store.fingerprint(fingerprint)
        span = dict(kind='lines-v1', path=fp['path'], sourceSha256=fp['source_sha'], startLine=1, endLine=1,
                    spanSha256='d' * 64)
        cards = [dict(wiki_path='cards/a.md', ordinal=0, fingerprint_id=fingerprint, spans=[span]),
                 dict(wiki_path='cards/a.md', ordinal=1, fingerprint_id=fingerprint,
                      spans=[dict(span, startLine=2, endLine=2)])]
        with store._session():
            rows, _objects = store._rows_card_source_objects(cards)
        self.assertEqual([(r['card_path'], r['source_path']) for r in rows], [('cards/a.md', 'notes/src.md')])
        self.assertEqual([s['startLine'] for s in store._rows_card_source_spans(rows[0])], [1, 2])
        other = store.put_fingerprint(dict(fp, source_sha='0' * 64, scan_id='2' * 32))
        cards[1] = dict(cards[1], fingerprint_id=other)
        with store._session():
            self.assertEqual(self.refused(store._rows_card_source_objects, cards).reason, 'card_source_conflict')


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(CardSourceChecks):
        if name in CardSourceChecks.__dict__:
            suite.addTest(CardSourceChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
