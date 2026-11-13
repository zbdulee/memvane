#!/usr/bin/env python3
"""Row-format (4) card source rows: a trusted card's source spans as view intervals.

Card source rows use ``s_card_sources`` with stage changes, rollback deltas,
the member accumulator and GC. Synthetic stores only.
"""
import hashlib
import importlib.util
import json
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

    def test_02_byte_spans_refuse(self):
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

    def test_06_one_source_path_folds_into_the_lowest_ordinal_observation(self):
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
        first = dict(cards[0])
        # Two observations of one path fold into the lowest ordinal's, whatever the input order.
        cards = [dict(cards[1], fingerprint_id=other), first]
        with store._session():
            rows, objects = store._rows_card_source_objects(cards)
            single, _ = store._rows_card_source_objects([first])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['observation_id'], single[0]['observation_id'])
        self.assertEqual(len(objects), 1)
        self.assertEqual([s['startLine'] for s in store._rows_card_source_spans(rows[0])], [1, 2])


    OLDER = b'# Source\nolder source body\n'

    def two_revision_card(self, *, current_first=False):
        """A verified writer card whose ``sourceRevisions`` cite ``notes/src.md`` at two shas
        (an older one and the file's current bytes, listed older first unless ``current_first``)."""
        from memvane.wiki.card import yaml_scalars
        raw = (self.root / 'notes/src.md').read_bytes()
        older = self.OLDER
        listed = (raw, older) if current_first else (older, raw)
        lines = yaml_scalars.dump_source_revisions([
            dict(kind='file', path='notes/src.md', collection='fixture', sha256=hashlib.sha256(data).hexdigest(),
                 size=len(data), mtimeNs=1) for data in listed])
        spans = yaml_scalars.dump_source_spans([
            dict(path='notes/src.md', sourceSha256=hashlib.sha256(data).hexdigest(), startLine=start, endLine=end,
                 spanSha256=hashlib.sha256(data).hexdigest()) for data, start, end in ((older, 3, 4), (raw, 1, 2))])
        text = ('---\ntitle: "Two revisions"\nstatus: verified\ncreatedBy: memvane\nsourceRevisions:\n'
                + ''.join('  - ' + line + '\n' for line in lines) + 'sourceSpans:\n'
                + ''.join('  - ' + line + '\n' for line in spans) + '---\n# Two revisions\ntworevprobe body\n')
        (self.wiki() / 'cards' / 'a.md').write_text(text, encoding='utf8')

    def ingest_card(self, store, handle, path='cards/a.md', **kwargs):
        """``ingest_attested_card`` of one writer card; ``kwargs`` pass through."""
        decision = {'projectRoot': str(self.root), 'scopeId': self.scope,
                    'collections': [{'name': 'fixture', 'role': 'raw', 'sourceRoot': str(self.root)},
                                    {'name': 'fixture-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki())}],
                    'collectionRoots': {'fixture': str(self.root), 'fixture-wiki': str(self.wiki())},
                    'recallSettings': {'skipPaths': []}}
        return store.ingest_attested_card(handle.worktree_id, path, decision=decision,
                                          provenance_scan={'id': '3' * 32, 'observed_ns': 5}, **kwargs)

    def attested(self, store, admitted):
        """``[(path, source_sha, byte_length)]`` of an admitted card's attestation sources."""
        spec = store.attestation(admitted['trust_digest'])
        return [tuple(store.fingerprint(s['fingerprint_id'])[k] for k in ('path', 'source_sha', 'byte_length'))
                for s in spec['sources']]

    def publish_admitted(self, store, handle, admitted):
        manifest = self.snapshot(store, handle, trust=False)
        manifest['members'] = [dict(path='cards/a.md', blob_sha=admitted['blob_sha'], display_title='Two',
                                    trust_digest=admitted['trust_digest'])]
        return self.publish_manifest(store, handle, manifest)

    def publish_two_revision_card(self, store, handle):
        """Ingest the card without a current observation and publish it as the only member."""
        admitted = self.ingest_card(store, handle)
        self.assertEqual([row[0] for row in self.attested(store, admitted)], ['notes/src.md', 'notes/src.md'])
        return self.publish_admitted(store, handle, admitted)

    def two_stores(self):
        """Two independent row-format stores over one worktree registration."""
        # Fingerprints carry the worktree id, so equal attestations need the same worktree.
        repository, worktree = self.bind()
        stores = []
        for path in (self.state / 'first.db', self.db):
            store = self.track(self.Store.create(str(path), project_root=str(self.root), repository=repository,
                                                 worktree=worktree, scope_id=self.scope))
            stores.append((store, store.capture_view(worktree['worktreeId'])))
        return stores

    def source_rows(self, store, handle):
        """Card source rows of the head: ``card_sources`` ordinals or ``s_card_sources`` rows."""
        return len(self.card_source_rows(store, handle))

    def recall_output(self, store, handle):
        """What recall consumes: searched rows, their card locations and the cards' trust."""
        pin = store.pin_view(handle)
        try:
            rows = [{k: row[k] for k in ('path', 'blob_sha', 'body', 'display_title', 'score')}
                    for row in store.search(pin, 'tworevprobe', 10)]
            locations = store.card_locations(pin, ['cards/a.md'])
        finally:
            store.release(pin)
        trust = [tuple(r) for r in store._db.execute(
            'SELECT path,trust_digest,eligible FROM s_cards WHERE view_id=? AND gen_to IS NULL ORDER BY path',
            (handle.view_id,))]
        return dict(rows=rows, locations=locations, trust=trust)

    def test_07_writer_legacy_card_citing_one_path_at_two_revisions_publishes_in_two_stores(self):
        """The product producer reaches two observations of one source path.

        ``ingest_attested_card`` turns each frontmatter ``sourceRevisions``
        entry into a ``legacy_unbound`` fingerprint and dedupes only equal
        fingerprints; the reader rule it applies (``current_source=False``)
        does not compare revisions with the file, so one path at two shas is
        one attestation with two sources of that path.
        """
        self.two_revision_card()
        results = {}
        (generation_store, generation_handle), (row_store, row_handle) = self.two_stores()
        for name, store, handle in (('first', generation_store, generation_handle),
                                    ('row', row_store, row_handle)):
            self.publish_two_revision_card(store, handle)
            results[name] = self.recall_output(store, handle)
        generation = results['first']
        self.assertEqual([row['path'] for row in generation['rows']], ['cards/a.md'])
        self.assertEqual(generation['locations'],
                         {'cards/a.md': ['notes/src.md#L1-L2', 'notes/src.md#L3-L4']})
        self.assertEqual(generation['trust'][0][2], 1)
        self.assertEqual(json.dumps(results['row'], sort_keys=True).encode('utf8'),
                         json.dumps(generation, sort_keys=True).encode('utf8'))
        self.assertEqual(len(self.card_source_rows(row_store, row_handle)), 1)

    # Writer ingest judges one path cited at two revisions by the current source (user decision 11/13 08:45).

    def current(self, data=None):
        data = (self.root / 'notes/src.md').read_bytes() if data is None else data
        return {'notes/src.md': (hashlib.sha256(data).hexdigest(), len(data))}

    def test_09_two_revisions_keep_only_the_one_matching_the_current_source(self):
        self.two_revision_card()
        raw = (self.root / 'notes/src.md').read_bytes()
        outputs = []
        for store, handle in self.two_stores():
            admitted = self.ingest_card(store, handle, current_sources=self.current())
            self.assertEqual(self.attested(store, admitted),
                             [('notes/src.md', hashlib.sha256(raw).hexdigest(), len(raw))])
            self.assertEqual(sum(len(s['spans']) for s in store.attestation(admitted['trust_digest'])['sources']), 1)
            self.publish_admitted(store, handle, admitted)
            output = self.recall_output(store, handle)
            self.assertEqual(output['locations'], {'cards/a.md': ['notes/src.md#L1-L2']})
            self.assertEqual(output['trust'][0][2], 1)
            self.assertEqual(self.source_rows(store, handle), 1)
            outputs.append(json.dumps(output, sort_keys=True).encode('utf8'))
        self.assertEqual(outputs[0], outputs[1])

    def test_10_the_listing_order_does_not_pick_the_revision(self):
        raw = (self.root / 'notes/src.md').read_bytes()
        for store, handle in self.two_stores():
            kept = []
            for current_first in (False, True):
                self.two_revision_card(current_first=current_first)
                admitted = self.ingest_card(store, handle, current_sources=self.current())
                kept.append(self.attested(store, admitted))
            self.assertEqual(kept, [[('notes/src.md', hashlib.sha256(raw).hexdigest(), len(raw))]] * 2)

    def test_11_no_revision_matching_the_current_source_admits_the_card_untrusted(self):
        self.two_revision_card()
        third = b'# Source\nthird source body\n'
        (self.root / 'notes/src.md').write_bytes(third)
        for store, handle in self.two_stores():
            before = store.counts()['card_attestations']
            admitted = self.ingest_card(store, handle, current_sources=self.current(third))
            self.assertIsNone(admitted['trust_digest'])
            self.assertEqual(store.counts()['card_attestations'], before)
            self.publish_admitted(store, handle, admitted)
            output = self.recall_output(store, handle)
            self.assertEqual((output['rows'], output['locations']), ([], {}))
            self.assertEqual(output['trust'], [('cards/a.md', None, 0)])
            self.assertEqual(self.source_rows(store, handle), 0)

    def test_12_without_a_current_observation_both_revisions_stay(self):
        """Regression guard: no observation (a scan over its bounds) picks no winner."""
        self.two_revision_card()
        for store, handle in self.two_stores():
            self.publish_two_revision_card(store, handle)
            output = self.recall_output(store, handle)
            self.assertEqual(output['locations'], {'cards/a.md': ['notes/src.md#L1-L2', 'notes/src.md#L3-L4']})
            self.assertEqual(output['trust'][0][2], 1)

    def test_13_a_path_outside_the_observation_is_not_judged(self):
        self.two_revision_card()
        unobserved = {'notes/other.md': ('0' * 64, 1)}
        for store, handle in self.two_stores():
            judged = self.ingest_card(store, handle, current_sources=unobserved)
            plain = self.ingest_card(store, handle)
            self.assertEqual(judged, plain)
            self.assertEqual([row[0] for row in self.attested(store, judged)], ['notes/src.md', 'notes/src.md'])

    def test_14_a_card_citing_its_path_once_is_unchanged_even_when_stale(self):
        from memvane.wiki.card import yaml_scalars
        raw = (self.root / 'notes/src.md').read_bytes()
        line = yaml_scalars.dump_source_revisions([dict(
            kind='file', path='notes/src.md', collection='fixture', sha256=hashlib.sha256(self.OLDER).hexdigest(),
            size=len(self.OLDER), mtimeNs=1)])[0]
        (self.wiki() / 'cards' / 'one.md').write_text(
            '---\ntitle: "One revision"\nstatus: verified\ncreatedBy: memvane\nsourceRevisions:\n  - ' + line
            + '\n---\n# One revision\nonerevprobe body\n', encoding='utf8')
        for store, handle in self.two_stores():
            for current in (self.current(raw), self.current(self.OLDER)):
                judged = self.ingest_card(store, handle, 'cards/one.md', current_sources=current)
                self.assertIsNotNone(judged['trust_digest'])
                self.assertEqual(judged, self.ingest_card(store, handle, 'cards/one.md'))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(CardSourceChecks):
        if name in CardSourceChecks.__dict__:
            suite.addTest(CardSourceChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
