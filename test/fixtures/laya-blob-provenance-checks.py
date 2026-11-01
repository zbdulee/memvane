"""Metadata-only provenance contracts, isolated from operational source data."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('blob_set_helpers', HERE / 'laya-blob-sets-checks.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class ProvenanceChecks(unittest.TestCase):
    setUp = helpers.SetChecks.setUp

    def fp(self, **changes):
        value = dict(worktree_id=self.worktree['worktreeId'], branch_key='non-git',
                     path='notes/source.md', source_sha='a' * 64, byte_length=40,
                     head_oid=None, tracked_state='unknown', binding_state='observed',
                     scan_id='1' * 32, observed_ns=1)
        return dict(value, **changes)

    def evidence(self, fingerprint, **changes):
        value = dict(blob_sha=self.blob, verification_kind='compiled', engine_identity='synthetic:codex',
                     policy_digest='b' * 64,
                     receipt=dict(schema='memvane-card-evidence-v1', verdict='verified',
                                  result_digest='c' * 64, prior_trust=None),
                     sources=[dict(fingerprint_id=fingerprint, spans=[dict(start=0, end=4)])])
        return dict(value, **changes)

    def test_01_fingerprint_is_canonical_and_exact_replay_noop(self):
        value = self.fp(); first = self.store.put_fingerprint(value)
        self.assertEqual(first, self.store.put_fingerprint(dict(reversed(list(value.items())))))
        self.assertEqual(self.store.fingerprint(first), value)
        self.assertEqual(self.store.counts()['source_fingerprints'], 1)

    def test_02_changed_source_and_scan_leave_old_fingerprint(self):
        old = self.fp(); first = self.store.put_fingerprint(old)
        changed = self.fp(source_sha='d' * 64, scan_id='2' * 32, observed_ns=2)
        second = self.store.put_fingerprint(changed)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.fingerprint(first), old)
        self.assertEqual(self.store.fingerprint(second), changed)

    def test_03_invalid_metadata_is_rejected_without_rows(self):
        bad = [('worktree_id', '0' * 32), ('branch_key', 'refs/heads/main'),
               ('path', '../raw.md'), ('path', '/raw.md'), ('path', '.hidden/x.md'),
               ('source_sha', 'BAD'), ('byte_length', True), ('byte_length', -1),
               ('observed_ns', float('inf')), ('observed_ns', 10 ** 400),
               ('head_oid', 'a' * 40), ('tracked_state', 'yes'),
               ('binding_state', 'assumed'), ('scan_id', 'bad'), ('body', 'RAW')]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                row = self.fp(); row[key] = value
                with self.assertRaises(self.Error): self.store.put_fingerprint(row)
        self.assertEqual(self.store.counts()['source_fingerprints'], 0)

    def test_04_attestation_replay_and_canonical_roundtrip(self):
        fp = self.store.put_fingerprint(self.fp()); evidence = self.evidence(fp)
        first = self.store.put_attestation(evidence)
        self.assertEqual(first, self.store.put_attestation(dict(reversed(list(evidence.items())))))
        self.assertEqual(self.store.attestation(first), evidence)
        self.assertEqual(self.store.counts()['card_attestations'], 1)
        self.assertEqual(self.store.counts()['attestation_sources'], 1)

    def test_05_one_blob_can_have_two_distinct_source_attestations(self):
        fp1 = self.store.put_fingerprint(self.fp())
        fp2 = self.store.put_fingerprint(self.fp(source_sha='d' * 64, observed_ns=2))
        first = self.store.put_attestation(self.evidence(fp1))
        second = self.store.put_attestation(self.evidence(fp2))
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.attestation(first)['sources'][0]['fingerprint_id'], fp1)
        self.assertEqual(self.store.attestation(second)['sources'][0]['fingerprint_id'], fp2)
        self.assertEqual(self.store.counts()['wiki_blobs'], 1)

    def test_06_spans_are_bounded_integer_metadata_not_excerpts(self):
        fp = self.store.put_fingerprint(self.fp())
        bad = [[dict(start=-1, end=2)], [dict(start=0, end=41)], [dict(start=0, end=0)],
               [dict(start=True, end=2)], [dict(start=0, end=4, excerpt='RAW')],
               [dict(start=4, end=8), dict(start=2, end=3)],
               [dict(start=0, end=8), dict(start=4, end=9)]]
        for spans in bad:
            with self.subTest(spans=spans):
                with self.assertRaises(self.Error):
                    self.store.put_attestation(self.evidence(fp, sources=[dict(fingerprint_id=fp, spans=spans)]))
        self.assertEqual(self.store.counts()['card_attestations'], 0)

    def test_07_receipt_refuses_raw_transcript_and_unknown_fields(self):
        fp = self.store.put_fingerprint(self.fp())
        for key, value in [('body', 'RAW'), ('transcript', 'RAW'), ('schema', 'other'),
                           ('verdict', 'assumed'), ('result_digest', 'bad'), ('prior_trust', 'd' * 64)]:
            with self.subTest(key=key):
                row = self.evidence(fp); row['receipt'][key] = value
                with self.assertRaises(self.Error): self.store.put_attestation(row)

    def test_08_missing_source_never_leaves_partial_attestation(self):
        fp = self.store.put_fingerprint(self.fp()); row = self.evidence(fp)
        row['sources'].append(dict(fingerprint_id='e' * 64, spans=[]))
        with self.assertRaises(self.Error): self.store.put_attestation(row)
        self.assertEqual(self.store.counts()['card_attestations'], 0)
        self.assertEqual(self.store.counts()['attestation_sources'], 0)

    def test_09_legacy_unbound_is_preserved_without_compile_promotion(self):
        value = self.fp(binding_state='legacy_unbound')
        fp = self.store.put_fingerprint(value)
        row = self.evidence(fp, verification_kind='legacy_verified')
        trust = self.store.put_attestation(row)
        self.assertEqual(self.store.attestation(trust), row)
        self.assertEqual(self.store.fingerprint(fp)['binding_state'], 'legacy_unbound')
        with self.assertRaises(self.Error): self.store.put_attestation(self.evidence(fp))

    def test_10_original_source_body_is_not_read_or_stored(self):
        raw = b'SOURCE_CANARY_NEVER_STORED_3736c9b1'
        source = self.project / 'notes/source.md'; source.parent.mkdir()
        source.write_bytes(raw)
        module = sys.modules['laya_blob_store']; regular = module._regular
        def no_source(root, path, limit):
            self.assertNotIn('source.md', str(root) + path)
            return regular(root, path, limit)
        with mock.patch.object(module, '_regular', side_effect=no_source):
            fp = self.store.put_fingerprint(self.fp(source_sha=hashlib.sha256(raw).hexdigest(), byte_length=len(raw)))
            trust = self.store.put_attestation(self.evidence(fp))
        self.assertNotIn(raw.decode(), json.dumps(self.store.attestation(trust)))
        self.assertNotIn(raw, self.path.read_bytes())

    def test_11_metadata_rows_are_sql_immutable_and_no_replace(self):
        fp = self.store.put_fingerprint(self.fp()); self.store.put_attestation(self.evidence(fp))
        with sqlite3.connect(self.path) as db:
            for table in ('source_fingerprints', 'card_attestations', 'attestation_sources'):
                for sql in (f'UPDATE {table} SET rowid=rowid', f'DELETE FROM {table}',
                            f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
                    with self.subTest(table=table, sql=sql):
                        with self.assertRaises(sqlite3.Error): db.execute(sql)
                    db.rollback()

    def test_12_semantic_tamper_is_detected_even_after_schema_restored(self):
        fp = self.store.put_fingerprint(self.fp())
        with sqlite3.connect(self.path) as db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='source_fingerprints_update'").fetchone()[0]
            db.execute('DROP TRIGGER source_fingerprints_update')
            db.execute('UPDATE source_fingerprints SET source_sha=?', ('d' * 64,))
            db.execute(trigger); db.commit()
        with self.assertRaises(self.Error): self.store.fingerprint(fp)
        with self.assertRaises(self.Error): self.store.verify()

    def test_13_schema_tamper_is_refused_before_new_write(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER wiki_blobs_update'); db.commit()
        with self.assertRaises(self.Error): self.store.put_fingerprint(self.fp())

    def test_14_sql_source_link_failure_rolls_back_parent(self):
        fp = self.store.put_fingerprint(self.fp())
        def deny(action, name, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and name == 'attestation_sources' else sqlite3.SQLITE_OK
        self.store._db.set_authorizer(deny)
        try:
            with self.assertRaises(self.Error): self.store.put_attestation(self.evidence(fp))
        finally: self.store._db.set_authorizer(None)
        self.assertEqual(self.store.counts()['card_attestations'], 0)
        self.assertEqual(self.store.counts()['attestation_sources'], 0)

    def git_pair(self):
        import laya_checkout_view as checkout
        main, linked = self.root / 'git', self.root / 'linked'
        (main / '.memvane/wiki').mkdir(parents=True)
        (main / '.memvane/settings.json').write_text(json.dumps({'name': 'fixture'}), encoding='utf8')
        (main / '.memvane/wiki/card.md').write_text(self.body, encoding='utf8')
        env = {k: val for k, val in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        for args in [('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-q', '-m', 'fixture'),
                     ('worktree', 'add', '-q', '-b', 'side', str(linked))]:
            r = subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull, *args],
                               cwd=main, env=env, encoding='utf8', capture_output=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
        repo = checkout.register_repository(str(main))
        a, b = checkout.register_worktree(str(main), repo), checkout.register_worktree(str(linked), repo)
        store = self.Store.create(self.state / 'rebound.sqlite', project_root=str(main),
                                  repository=repo, worktree=a, scope_id='e' * 32)
        self.addCleanup(store.close); store.register_worktree(str(linked), b)
        blob = store.ingest_card(a['worktreeId'], 'card.md')
        head = checkout.capture_view(str(main), repo, a)['headOid']
        return store, a, b, head, blob

    def rebound_setup(self):
        store, a, b, head, blob = self.git_pair()
        first = self.fp(worktree_id=a['worktreeId'], branch_key='refs/heads/main', head_oid=head, tracked_state='tracked')
        fp = store.put_fingerprint(first)
        original = self.evidence(fp, blob_sha=blob)
        trust = store.put_attestation(original)
        second = dict(first, worktree_id=b['worktreeId'], branch_key='refs/heads/side', scan_id='2' * 32, observed_ns=2)
        return store, blob, trust, original, second

    def rebound_evidence(self, fp, blob, prior):
        row = self.evidence(fp, blob_sha=blob, verification_kind='rebound')
        row['receipt']['prior_trust'] = prior
        return row

    def test_15_rebound_is_a_new_attestation_with_separate_worktree(self):
        store, blob, prior, original, second = self.rebound_setup()
        fp = store.put_fingerprint(second)
        row = self.rebound_evidence(fp, blob, prior); trust = store.put_attestation(row)
        self.assertNotEqual(trust, prior)
        self.assertEqual(store.attestation(prior), original)
        self.assertEqual(store.attestation(trust), row)
        self.assertEqual(store.fingerprint(fp)['worktree_id'], second['worktree_id'])
        self.assertTrue(store.verify())

    def test_16_rebound_rejects_unknown_or_mismatched_source(self):
        store, blob, prior, _, second = self.rebound_setup()
        for changes in [dict(head_oid=None), dict(source_sha='d' * 64), dict(byte_length=41),
                        dict(binding_state='legacy_unbound'), dict(tracked_state='unknown')]:
            with self.subTest(changes=changes):
                fp = store.put_fingerprint(dict(second, **changes))
                with self.assertRaises(self.Error): store.put_attestation(self.rebound_evidence(fp, blob, prior))
        self.assertEqual(store.counts()['card_attestations'], 1)

    def test_17_rebound_rejects_wrong_prior_blob_policy_or_engine(self):
        store, blob, prior, _, second = self.rebound_setup(); fp = store.put_fingerprint(second)
        (self.root / 'linked/.memvane/wiki/different.md').write_text('# Different\ncard', encoding='utf8')
        other_blob = store.ingest_card(second['worktree_id'], 'different.md')
        self.assertNotEqual(other_blob, blob)
        for changes in [dict(policy_digest='d' * 64), dict(engine_identity='other'), dict(blob_sha=other_blob)]:
            with self.subTest(changes=changes):
                row = self.rebound_evidence(fp, blob, prior); row.update(changes)
                with self.assertRaises(self.Error): store.put_attestation(row)

    def test_18_duplicate_sources_and_excess_spans_are_refused(self):
        fp = self.store.put_fingerprint(self.fp()); row = self.evidence(fp)
        row['sources'] *= 2
        with self.assertRaises(self.Error): self.store.put_attestation(row)
        row = self.evidence(fp, sources=[dict(fingerprint_id=fp, spans=[dict(start=0, end=1)] * 257)])
        with self.assertRaises(self.Error): self.store.put_attestation(row)

    def test_19_verifier_covers_blob_sets_and_metadata_together(self):
        r = self.store.register_recipe(helpers.recipe()); p = self.store.register_profile(helpers.profile())
        self.store.put_chunk_set(self.blob, r, [helpers.chunk(self.body)])
        self.store.put_embedding_set(self.blob, r, p, [dict(seq=0, input_sha=self.store.vector_input_sha(self.blob, r, p, 0),
                                                          embedding=helpers.struct.pack('<ff', 1, 2))])
        fp = self.store.put_fingerprint(self.fp()); self.store.put_attestation(self.evidence(fp))
        self.assertTrue(self.store.verify())

    def test_20_zero_source_candidate_receipt_does_not_enable_rebind(self):
        row = self.evidence('0' * 64, sources=[])
        trust = self.store.put_attestation(row)
        self.assertEqual(self.store.attestation(trust)['sources'], [])
        row['verification_kind'] = 'rebound'; row['receipt']['prior_trust'] = trust
        with self.assertRaises(self.Error): self.store.put_attestation(row)


if __name__ == '__main__':
    unittest.main()
