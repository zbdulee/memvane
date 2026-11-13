"""Metadata-only provenance contracts, isolated from operational source data."""
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
OLD_SETTINGS_COLUMNS = ("settings_path TEXT NOT NULL CHECK(typeof(settings_path)='text'), settings_sha TEXT NOT NULL "
                        "CHECK(typeof(settings_sha)='text' AND length(settings_sha)=64 AND settings_sha NOT GLOB '*[^0-9a-f]*'), ")
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
               ('path', '../raw.md'), ('path', '/raw.md'), ('path', './x.md'),
               ('path', 'a/./b.md'), ('path', 'a/../b.md'), ('path', 'a//b.md'), ('path', 'a/'),
               ('path', ''), ('path', '.'), ('path', '..'), ('path', 'a\\b.md'), ('path', 'a\x00b.md'),
               ('path', 'a\nb.md'), ('path', 'x' * 4097),
               ('source_sha', 'BAD'), ('byte_length', True), ('byte_length', -1),
               ('observed_ns', float('inf')), ('observed_ns', 10 ** 400),
               ('head_oid', 'a' * 40), ('tracked_state', 'yes'),
               ('binding_state', 'assumed'), ('scan_id', 'bad'), ('body', 'RAW')]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                row = self.fp(); row[key] = value
                with self.assertRaises(self.Error): self.store.put_fingerprint(row)
        self.assertEqual(self.store.counts()['source_fingerprints'], 0)

    def test_03b_source_path_under_dot_directory_roundtrips(self):
        """Raw roots may sit in dot directories (``.nova/06_Sessions``): the
        fingerprint and manifest source path rule refuses only empty, ``.``
        and ``..`` segments. Wiki card paths keep refusing dot segments."""
        values = sys.modules['laya_blob_values']
        view_values = __import__('memvane.store.view_values')
        for path in ('.nova/06_Sessions/ep01.md', '.hidden/x.md', '.nova/x/.drafts/a.md', '..a/b.md'):
            with self.subTest(path=path):
                key = self.store.put_fingerprint(self.fp(path=path))
                self.assertEqual(self.store.fingerprint(key), self.fp(path=path))
                self.assertEqual(values.source_path(path), path)
        manifest = dict(scan_id='1' * 32, policy_digest='b' * 64, revocation_epoch=0, complete=True,
                        sources=[dict(path='.nova/06_Sessions/ep01.md', fingerprint_id='d' * 64,
                                      presence='present')], members=[])
        self.assertEqual(view_values.manifest(manifest, 'b' * 64)['sources'], manifest['sources'])
        for path in ('.hidden/x.md', '.nova/card.md'):
            with self.subTest(card=path):
                with self.assertRaises(self.Error) as caught:
                    values.relative(path)
                self.assertEqual(caught.exception.reason, 'invalid_relative_path')
                with self.assertRaises(self.Error):
                    view_values.card_path(path)
        # Same digest domain and canonical form: a plain path's key is the
        # key the previous rule produced.
        plain = values.fingerprint(self.fp(worktree_id='0' * 32), None)
        self.assertEqual(values.digest('memvane-source-fingerprint-v1', plain),
                         'd488f4b0d8c30f90a93fca16c1ffd7f8ce0caf8beebec01d997175c1e81c1bba')
        plain_manifest = dict(manifest, sources=[dict(manifest['sources'][0], path='notes/source.md')],
                              members=[dict(path='concepts/a.md', blob_sha='e' * 64, display_title='A',
                                            trust_digest=None)])
        self.assertEqual(view_values.manifest_digest(view_values.manifest(plain_manifest, 'b' * 64)),
                         '885dd7b131d89f2a8de1262995449812ea9ab2e4202f637dc760c99818c3a581')

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

    def test_legacy_line_spans_keep_their_original_evidence_level(self):
        values = sys.modules['laya_blob_values']
        detached = self.fp(branch_key='detached:' + '1' * 40,
                           binding_state='legacy_unbound', tracked_state='unknown')
        self.assertEqual(values.fingerprint(detached, 'sha1'), detached)
        for changes in ({'binding_state': 'observed'}, {'tracked_state': 'tracked'},
                        {'branch_key': 'detached:not-an-oid'}):
            with self.subTest(detached=changes), self.assertRaises(self.Error):
                values.fingerprint(dict(detached, **changes), 'sha1')
        fingerprint = self.store.put_fingerprint(self.fp(binding_state='legacy_unbound'))
        span = {'kind': 'legacy-lines-v1', 'path': 'notes/source.md',
                'sourceSha256': 'a' * 64, 'startLine': 3, 'endLine': 5,
                'spanSha256': 'd' * 64}
        row = self.evidence(fingerprint, verification_kind='legacy_verified')
        row['sources'][0]['spans'] = [span]
        trust = self.store.put_attestation(row)
        self.assertEqual(self.store.attestation(trust), row)
        self.assertTrue(self.store.verify())
        for bad_span in ({**span, 'path': 'notes/other.md'},
                         {**span, 'sourceSha256': 'f' * 64},
                         {**span, 'spanSha256': 'invalid'},
                         {**span, 'endLine': 2}):
            bad = self.evidence(fingerprint, verification_kind='legacy_verified')
            bad['sources'][0]['spans'] = [bad_span]
            with self.subTest(span=bad_span), self.assertRaises(self.Error):
                self.store.put_attestation(bad)
        second = self.store.put_fingerprint(self.fp(binding_state='legacy_unbound', path='notes/other.md'))
        too_many = self.evidence(fingerprint, verification_kind='legacy_verified')
        too_many['sources'][0]['spans'] = [{**span, 'startLine': i, 'endLine': i} for i in range(1, 10)]
        too_many['sources'].append({'fingerprint_id': second, 'spans': [
            {**span, 'path': 'notes/other.md', 'startLine': i, 'endLine': i} for i in range(1, 10)]})
        with self.assertRaises(self.Error):
            self.store.put_attestation(too_many)
        compiled = self.evidence(fingerprint)
        compiled['sources'][0]['spans'] = [span]
        with self.assertRaises(self.Error):
            self.store.put_attestation(compiled)

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
        original = self.store._fts_guard._authorize
        def deny(action, name, *args):
            if action == sqlite3.SQLITE_INSERT and name == 'attestation_sources':
                return sqlite3.SQLITE_DENY
            return original(action, name, *args)
        self.store._db.set_authorizer(deny)
        try:
            with self.assertRaises(self.Error): self.store.put_attestation(self.evidence(fp))
        finally: self.store._db.set_authorizer(original)
        self.assertEqual(self.store.counts()['card_attestations'], 0)
        self.assertEqual(self.store.counts()['attestation_sources'], 0)

    def git_pair(self):
        from memvane.store import checkout_view as checkout
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

    # Writer ingest of the existing frontmatter trust contract (K2 (a)).
    SCAN = {'id': '3' * 32, 'observed_ns': 5}

    def legacy_decision(self, skip=()):
        return {'projectRoot': str(self.project), 'scopeId': self.scope,
                'collections': [{'name': 'fixture', 'role': 'raw', 'sourceRoot': str(self.project)},
                                {'name': 'fixture-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}],
                'collectionRoots': {'fixture': str(self.project), 'fixture-wiki': str(self.wiki)},
                'recallSettings': {'skipPaths': list(skip)}}

    def legacy_card(self, name, *, status='verified', title='"Legacy card"', before='',
                    body_hash=True, edit=None):
        import yaml_scalars
        raw = b'Synthetic source for legacy evidence.\n'
        source = self.project / 'notes' / 'source.md'
        source.parent.mkdir(exist_ok=True); source.write_bytes(raw)
        revision = dict(kind='file', path='notes/source.md', collection='fixture',
                        sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                        mtimeNs=source.stat().st_mtime_ns)
        line = yaml_scalars.dump_source_revisions([revision])[0]
        rest = '# Legacy card\nlegacyprobe verified body\n'
        front = (f'title: {title}\n{before}status: {status}\ncreatedBy: memvane\n'
                 f'sourceRevisions:\n  - {line}\n')
        text = '---\n' + front + '---\n' + rest
        if body_hash:
            text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                                + yaml_scalars.card_body_hash(text) + '\n', 1)
        if edit is not None:
            text = edit(text)
        (self.wiki / name).write_text(text, encoding='utf8')
        return text

    def ingest_legacy(self, name, **kwargs):
        decision = kwargs.pop('decision', None) or self.legacy_decision()
        return self.store.ingest_attested_card(self.worktree['worktreeId'], name,
                                               decision=decision, provenance_scan=self.SCAN, **kwargs)

    def test_21_verified_frontmatter_ingest_creates_unbound_legacy_attestation(self):
        self.legacy_card('verified.md')
        result = self.ingest_legacy('verified.md')
        self.assertEqual(set(result), {'blob_sha', 'trust_digest'})
        self.assertEqual(result['blob_sha'], hashlib.sha256((self.wiki / 'verified.md').read_bytes()).hexdigest())
        spec = self.store.attestation(result['trust_digest'])
        self.assertEqual(spec['blob_sha'], result['blob_sha'])
        self.assertEqual(spec['verification_kind'], 'legacy_verified')
        self.assertEqual(spec['policy_digest'], self.store._policy)
        self.assertEqual(len(spec['sources']), 1)
        fp = self.store.fingerprint(spec['sources'][0]['fingerprint_id'])
        self.assertEqual((fp['binding_state'], fp['head_oid'], fp['tracked_state'], fp['branch_key']),
                         ('legacy_unbound', None, 'unknown', 'non-git'))
        self.assertEqual((fp['scan_id'], fp['observed_ns'], fp['path']), (self.SCAN['id'], 5, 'notes/source.md'))
        # Replay of the same bytes and scan is the same attestation, not a second row.
        self.assertEqual(self.ingest_legacy('verified.md'), result)
        self.assertEqual(self.store.counts()['card_attestations'], 1)
        # An absent body digest is "not bound" under the reader contract.
        self.legacy_card('unbound.md', body_hash=False)
        self.assertIsNotNone(self.ingest_legacy('unbound.md')['trust_digest'])
        self.assertTrue(self.store.verify())

    def test_22_untrusted_or_forged_frontmatter_ingests_without_attestation(self):
        cases = {
            'generated.md': dict(status='generated'),
            'first-wins.md': dict(status='verified', before='status: generated\n'),
            'quoted-newline.md': dict(status='generated', title='"x\\nstatus: verified"'),
            'body-edit.md': dict(edit=lambda text: text.replace('verified body', 'edited body')),
            'bad-hash.md': dict(edit=lambda text: re.sub(r'verifiedBodyHash: [0-9a-f]{64}',
                                                         'verifiedBodyHash: ' + 'f' * 64, text, count=1)),
            'foreign.md': dict(edit=lambda text: text.replace('createdBy: memvane', 'createdBy: someone', 1)),
            'no-revisions.md': dict(edit=lambda text: text[:text.index('sourceRevisions:')]
                                    + text[text.index('\n---\n') + 1:]),
        }
        for name, options in cases.items():
            with self.subTest(name=name):
                text = self.legacy_card(name, **options)
                result = self.ingest_legacy(name)
                self.assertIsNone(result['trust_digest'])
                self.assertEqual(self.store.document(result['blob_sha'])['body'], text)
        self.legacy_card('skipped.md')
        self.assertIsNone(self.ingest_legacy('skipped.md', decision=self.legacy_decision(skip=['notes/']))['trust_digest'])
        self.assertEqual(self.store.counts()['card_attestations'], 0)
        self.assertEqual(self.store.counts()['source_fingerprints'], 0)

    def test_23_attested_ingest_refuses_foreign_decision_or_scan(self):
        self.legacy_card('verified.md')
        foreign = self.root / 'other'
        for decision in (dict(self.legacy_decision(), projectRoot=str(foreign)),
                         dict(self.legacy_decision(), scopeId='0' * 32),
                         dict(self.legacy_decision(), collections=[
                             {'name': 'fixture', 'role': 'raw', 'sourceRoot': str(self.project)},
                             {'name': 'fixture-wiki', 'role': 'wiki', 'sourceRoot': str(foreign)}])):
            with self.subTest(decision=decision), self.assertRaises(self.Error):
                self.ingest_legacy('verified.md', decision=decision)
        for scan in ({'id': 'bad', 'observed_ns': 1}, {'id': '3' * 32}, {'id': '3' * 32, 'observed_ns': True}):
            with self.subTest(scan=scan), self.assertRaises(self.Error):
                self.store.ingest_attested_card(self.worktree['worktreeId'], 'verified.md',
                                                decision=self.legacy_decision(), provenance_scan=scan)
        self.assertEqual(self.store.counts()['card_attestations'], 0)

    def normal_generation(self, members):
        views = importlib.import_module('memvane.store.view_store')
        view = views.ViewStore.open(self.path, repository=self.repository, scope_id=self.scope)
        self.addCleanup(view.close)
        handle = view.capture_view(self.worktree['worktreeId'])
        manifest = dict(scan_id=self.SCAN['id'], policy_digest=handle.policy_digest, revocation_epoch=0,
                        complete=True, sources=[], members=members)
        head = view.head(handle)
        gen = view.prepare_generation(handle, manifest, expected_head=head)
        view.publish_generation(handle, gen, expected_head=head)
        eligible = dict(view._db.execute('SELECT path,eligible FROM wiki_members WHERE gen_id=?', (gen,)))
        return view, handle, eligible

    def test_24_writer_legacy_trust_is_searchable_in_normal_generation(self):
        self.legacy_card('verified.md'); self.legacy_card('generated.md', status='generated')
        good, bad = self.ingest_legacy('verified.md'), self.ingest_legacy('generated.md')
        view, handle, eligible = self.normal_generation([
            dict(path='verified.md', blob_sha=good['blob_sha'], display_title='Verified',
                 trust_digest=good['trust_digest']),
            dict(path='generated.md', blob_sha=bad['blob_sha'], display_title='Generated',
                 trust_digest=bad['trust_digest'])])
        self.assertEqual(eligible, {'verified.md': 1, 'generated.md': 0})
        pin = view.pin_view(handle)
        try:
            self.assertEqual([row['path'] for row in view.search(pin, 'legacyprobe', 10)], ['verified.md'])
        finally:
            view.release(pin)

    def test_25_normal_generation_keeps_unbound_compiled_and_observed_legacy_ineligible(self):
        unbound = self.store.put_fingerprint(self.fp(binding_state='legacy_unbound'))
        observed = self.store.put_fingerprint(self.fp(observed_ns=2))
        legacy = self.store.put_attestation(self.evidence(unbound, verification_kind='legacy_verified',
                                                          policy_digest=self.store._policy))
        mixed = self.store.put_attestation(self.evidence(observed, verification_kind='legacy_verified',
                                                         policy_digest=self.store._policy))
        (self.wiki / 'copy.md').write_bytes((self.wiki / 'card.md').read_bytes())
        (self.wiki / 'second.md').write_text('# Second\nother card\n', encoding='utf8')
        second = self.store.ingest_card(self.worktree['worktreeId'], 'second.md')
        _, _, eligible = self.normal_generation([
            dict(path='card.md', blob_sha=self.blob, display_title='Legacy', trust_digest=legacy),
            dict(path='copy.md', blob_sha=self.blob, display_title='Observed', trust_digest=mixed),
            dict(path='second.md', blob_sha=second, display_title='None', trust_digest=None)])
        self.assertEqual(eligible, {'card.md': 1, 'copy.md': 0, 'second.md': 0})


    # P8-0 reservation: schema3 manifest before delete_receipts existed.
    PRE_RESERVATION_MANIFEST = 'e2bf4c70442a8987189f22e7d0708c1ba6682880114d969a51a5300b43b79510'

    def fixed_specs(self):
        receipt = lambda prior=None: dict(schema='memvane-card-evidence-v1', verdict='verified',
                                          result_digest='c' * 64, prior_trust=prior)
        base = dict(blob_sha='1' * 64, engine_identity='synthetic:codex', policy_digest='b' * 64)
        legacy = dict(kind='legacy-lines-v1', path='notes/source.md', sourceSha256='a' * 64,
                      startLine=3, endLine=5, spanSha256='d' * 64)
        return {
            'compiled': dict(base, verification_kind='compiled', receipt=receipt(), sources=[
                dict(fingerprint_id='2' * 64, spans=[dict(start=0, end=4), dict(start=4, end=9)])]),
            'compiled_empty': dict(base, verification_kind='compiled', receipt=receipt(), sources=[]),
            'rebound': dict(base, verification_kind='rebound', receipt=receipt('3' * 64), sources=[
                dict(fingerprint_id='4' * 64, spans=[dict(start=0, end=4)])]),
            'legacy': dict(base, verification_kind='legacy_verified', engine_identity='memvane:v6-reader',
                           receipt=receipt(), sources=[dict(fingerprint_id='5' * 64, spans=[legacy])]),
        }

    def line_span(self, **changes):
        value = dict(kind='lines-v1', path='notes/source.md', sourceSha256='a' * 64,
                     startLine=3, endLine=5, spanSha256='d' * 64)
        return dict(value, **changes)

    def compile_receipt(self, **changes):
        value = dict(schema='memvane-card-evidence-v2', verdict='verified', result_digest='c' * 64,
                     prior_trust=None, verdict_digest='e' * 64, verified_mode='cross-engine',
                     verify_engine='claude')
        return dict(value, **changes)

    def test_26_delete_receipts_table_is_reserved_append_only(self):
        from memvane.store import blob_schema as schema
        self.assertIn('delete_receipts', schema.TABLES)
        self.assertEqual(self.store.counts()['delete_receipts'], 0)
        row = dict(receipt_id='1' * 64, view_id='2' * 64, base_gen='g-x', path='notes/source.md',
                   fingerprint_id='3' * 64, observed_head=None, proof_json='{"schema":"x"}',
                   decision='hold', reason='head_unknown', created_ns=1)
        insert = ('INSERT INTO delete_receipts(' + ','.join(row) + ') VALUES('
                  + ','.join('?' for _ in row) + ')')
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE tbl_name='delete_receipts'")}
            self.assertTrue({'delete_receipts_duplicate', 'delete_receipts_update', 'delete_receipts_delete',
                             'delete_by_view'} <= names, names)
            db.execute(insert, tuple(row.values()))  # foreign keys are off on this raw connection
            db.commit()
            for bad in (dict(decision='delete'), dict(proof_json='not json'), dict(receipt_id='BAD'),
                        dict(observed_head=b'\x01')):
                with self.subTest(bad=bad), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(insert, tuple({**row, 'receipt_id': '4' * 64, **bad}.values()))
                db.rollback()
            for sql, args in (('UPDATE delete_receipts SET decision=?', ('retire',)),
                              ('DELETE FROM delete_receipts', ()),
                              ('INSERT OR REPLACE INTO delete_receipts SELECT * FROM delete_receipts', ()),
                              (insert, tuple(row.values()))):
                with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.Error, 'immutable_row'):
                    db.execute(sql, args)
                db.rollback()
            self.assertEqual(db.execute('SELECT count(*) FROM delete_receipts').fetchone()[0], 1)

    def test_27_store_created_with_reserved_manifest_reopens(self):
        from memvane.store import blob_schema as schema
        self.assertNotEqual(schema.expected_manifest(), self.PRE_RESERVATION_MANIFEST)
        self.store.close()
        store = self.Store.open(str(self.path), repository=self.repository, scope_id=self.scope)
        self.addCleanup(store.close)
        meta = store._db.execute('SELECT schema_version,schema_digest FROM store_meta').fetchone()
        self.assertEqual(tuple(meta), (3, schema.expected_manifest()))
        self.assertTrue(store.verify())

    def test_28_pre_reservation_schema3_store_is_refused_readonly(self):
        from memvane.store import blob_schema as schema
        # That store's worktree rows also carried the settings path and byte digest.
        old_ddl = [ddl.replace('policy_digest TEXT NOT NULL', OLD_SETTINGS_COLUMNS + 'policy_digest TEXT NOT NULL', 1)
                   if ddl.startswith('CREATE TABLE worktrees(') else ddl
                   for ddl in schema.statements() if 'delete_receipts' not in ddl]
        meta = dict(self.store._db.execute('SELECT * FROM store_meta').fetchone())
        trees = [dict(r, settings_path='/fixture/.memvane/settings.json', settings_sha='0' * 64)
                 for r in self.store._db.execute('SELECT * FROM worktrees')]
        path = self.state / 'pre-reservation.sqlite'
        with contextlib.closing(sqlite3.connect(path)) as db:
            for ddl in old_ddl:
                db.execute(ddl)
            self.assertEqual(schema.manifest(db), self.PRE_RESERVATION_MANIFEST)
            db.execute(f'PRAGMA application_id={schema.APP_ID}')
            db.execute(f'PRAGMA user_version={schema.SCHEMA_VERSION}')
            meta['schema_digest'] = self.PRE_RESERVATION_MANIFEST
            for table, rows in (('store_meta', [meta]), ('worktrees', trees)):
                for row in rows:
                    db.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES('
                               + ','.join('?' for _ in row) + ')', tuple(row.values()))
            db.commit()
        path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaises(self.Error) as caught:
            self.Store.open(str(path), repository=self.repository, scope_id=self.scope).close()
        self.assertEqual(caught.exception.reason, 'candidate_schema_mismatch')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + '-wal').exists())

    def test_29_existing_kind_trust_digests_are_unchanged(self):
        values = sys.modules['laya_blob_values']
        expected = {
            'compiled': ('ed0147ec42b3bd9f48837b59a62c000bdb50f075f26d50279fa59047bd80cc95',
                         '8f4cbb2e5ff758af53181379e3833e9699acee2f3057b5f939fcba08e48fdacf'),
            'compiled_empty': ('4c3994b5cb5e4e15bfa6787d4a7b6d5f28ae04281ac0fbc26a289d62265412f8',
                               '2509ee7e915d832fe8ecb8bea5381911b80c175438f0ea41a0a48ec3b8440496'),
            'rebound': ('b70dcb04bc2ea50de79e2bd6b4871f4eccfd3864ab9a2eb72c1042dd9cd3aa0c',
                        '0a275717ade509beef5e8830a2d0e4d12b98df8a56d8ed25acba57640b589bbf'),
            'legacy': ('f571075c6e9a331bc93ec8b35f7dc3127bf9ab7f329b587cb86cfc0c614589f8',
                       '46619e34b46b91ac609956db5a5e5d44e3a59a41b61435f8deaf4b91fc9b7d2a'),
        }
        for name, spec in self.fixed_specs().items():
            with self.subTest(name=name):
                normalized = values.attestation(copy.deepcopy(spec))
                self.assertEqual(normalized, spec)
                self.assertEqual((values.digest('memvane-card-attestation-v1', normalized),
                                  values.digest('memvane-card-provenance-v1', normalized['sources'])),
                                 expected[name])

    def test_30_compiled_line_span_kind_is_reserved_for_compiled_chain(self):
        values = sys.modules['laya_blob_values']
        specs = self.fixed_specs()
        accepted = []
        for kind, receipt_prior in (('compiled', None), ('rebound', '3' * 64)):
            spec = copy.deepcopy(specs['compiled'])
            spec['verification_kind'] = kind; spec['receipt']['prior_trust'] = receipt_prior
            spec['sources'][0]['spans'] = [self.line_span(), self.line_span(startLine=7, endLine=7)]
            accepted.append(spec)
        for spec in accepted:
            with self.subTest(kind=spec['verification_kind']):
                self.assertEqual(values.attestation(copy.deepcopy(spec)), spec)
        # the legacy kind still only belongs to legacy imports
        legacy_in_compiled = copy.deepcopy(accepted[0])
        legacy_in_compiled['sources'][0]['spans'] = [self.line_span(kind='legacy-lines-v1')]
        new_in_legacy = copy.deepcopy(specs['legacy'])
        new_in_legacy['sources'][0]['spans'] = [self.line_span()]
        bad = [new_in_legacy, legacy_in_compiled]
        for spans in ([self.line_span(), dict(start=0, end=4)], [dict(start=0, end=4), self.line_span()],
                      [self.line_span(), self.line_span()], [self.line_span(excerpt='RAW')],
                      [self.line_span(endLine=2)], [self.line_span(startLine=0)],
                      [self.line_span(spanSha256='bad')], [self.line_span(path='../x.md')],
                      [{k: v for k, v in self.line_span().items() if k != 'sourceSha256'}],
                      [self.line_span(startLine=i, endLine=i) for i in range(1, 18)]):
            spec = copy.deepcopy(accepted[0]); spec['sources'][0]['spans'] = spans
            bad.append(spec)
        too_many = copy.deepcopy(accepted[0])
        too_many['sources'] = [dict(fingerprint_id=c * 64, spans=[self.line_span(startLine=i, endLine=i)
                                    for i in range(1, 10)]) for c in '67']
        bad.append(too_many)
        for spec in bad:
            with self.subTest(spec=spec['sources'][:1]), self.assertRaises(self.Error):
                values.attestation(spec)

    def test_31_compile_receipt_fields_are_versioned_and_closed(self):
        values = sys.modules['laya_blob_values']
        specs = self.fixed_specs()
        for name in ('compiled', 'rebound'):
            spec = copy.deepcopy(specs[name])
            spec['receipt'] = self.compile_receipt(prior_trust=spec['receipt']['prior_trust'])
            with self.subTest(name=name):
                self.assertEqual(values.attestation(copy.deepcopy(spec)), spec)
        for mode in ('self', 'unknown'):
            spec = copy.deepcopy(specs['compiled']); spec['receipt'] = self.compile_receipt(verified_mode=mode)
            self.assertEqual(values.attestation(copy.deepcopy(spec))['receipt']['verified_mode'], mode)
        bad = []
        legacy = copy.deepcopy(specs['legacy']); legacy['receipt'] = self.compile_receipt(); bad.append(legacy)
        v1_extra = copy.deepcopy(specs['compiled']); v1_extra['receipt']['verdict_digest'] = 'e' * 64
        bad.append(v1_extra)
        for changes in (dict(verified_mode='assumed'), dict(verified_mode=''), dict(verdict_digest='bad'),
                        dict(verify_engine=''), dict(verify_engine='a\nb'), dict(verdict='pass'),
                        dict(schema='memvane-card-evidence-v3'), dict(body='RAW'), dict(transcript='RAW'),
                        dict(prior_trust='3' * 64)):
            spec = copy.deepcopy(specs['compiled']); spec['receipt'] = self.compile_receipt(**changes)
            bad.append(spec)
        missing = copy.deepcopy(specs['compiled']); missing['receipt'] = self.compile_receipt()
        del missing['receipt']['verdict_digest']; bad.append(missing)
        for spec in bad:
            with self.subTest(receipt=spec['receipt']), self.assertRaises(self.Error):
                values.attestation(spec)


    def test_32_store_binds_compiled_line_spans_to_observed_fingerprint(self):
        fp = self.store.put_fingerprint(self.fp())
        row = self.evidence(fp, receipt=self.compile_receipt())
        row['sources'][0]['spans'] = [self.line_span(), self.line_span(startLine=7, endLine=60)]
        trust = self.store.put_attestation(row)  # line numbers are not bounded by byte_length
        self.assertEqual(self.store.attestation(trust), row)
        self.assertTrue(self.store.verify())
        unbound = self.store.put_fingerprint(self.fp(binding_state='legacy_unbound', observed_ns=2))
        cases = [(fp, dict(path='notes/other.md')), (fp, dict(sourceSha256='f' * 64)), (unbound, {})]
        for fingerprint, changes in cases:
            with self.subTest(changes=changes, unbound=fingerprint == unbound):
                bad = self.evidence(fingerprint)
                bad['sources'][0]['spans'] = [self.line_span(**changes)]
                with self.assertRaises(self.Error) as caught:
                    self.store.put_attestation(bad)
                self.assertIn(caught.exception.reason, ('line_span_binding_invalid', 'unbound_source_not_compiled'))
        self.assertEqual(self.store.counts()['card_attestations'], 1)
        store, blob, _, original, second = self.rebound_setup()
        compiled = self.evidence(original['sources'][0]['fingerprint_id'], blob_sha=blob)
        compiled['sources'][0]['spans'] = [self.line_span()]
        prior = store.put_attestation(compiled)
        rebound = self.rebound_evidence(store.put_fingerprint(second), blob, prior)
        rebound['sources'][0]['spans'] = [self.line_span()]
        self.assertEqual(store.attestation(store.put_attestation(rebound)), rebound)
        self.assertTrue(store.verify())


if __name__ == '__main__':
    unittest.main()
