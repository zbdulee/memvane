"""Actual sync owner consumes compiled evidence on an unchanged view."""
from contextlib import closing
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('row_sync', HERE / 'laya-s-sync-rows-checks.py')
rows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rows)


class Contract:
    def test_unchanged_pass_consumes_intent_once(self):
        import laya_blob_values as values
        import laya_ledger_common as ledger
        import wiki_verify_worker
        self.select_schema3()
        self.card('cards/c3.md', 'c3source')
        result, _ = self.run_pass()
        self.assertEqual(result['state'], 'indexed')
        head = self.head()
        trust = self.member_trust(head[0], 'cards/c3.md')
        store = self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                                 scope_id=self.case.route['scopeId'])
        try:
            handle = store.capture_view(self.worktree['worktreeId'])
        finally:
            store.close()
        raw = (self.case.project / 'notes/c3source.md').read_bytes()
        card = (self.wiki / 'cards/c3.md').read_bytes()
        fp = dict(worktree_id=handle.worktree_id, branch_key=handle.branch_key,
                  path='notes/c3source.md', source_sha=hashlib.sha256(raw).hexdigest(), byte_length=len(raw),
                  head_oid=handle.head_oid, tracked_state='untracked', binding_state='observed',
                  scan_id='4'*32, observed_ns=4)
        observed = dict(schema='memvane-compile-source-fingerprints-v1', viewId=handle.view_id,
                        checkoutDigest=handle.checkout_digest, gitObjectFormat=self.repository['gitObjectFormat'],
                        scanId=fp['scan_id'], observedNs=fp['observed_ns'], fingerprints=[fp])
        data = dict(viewId=handle.view_id, targetPath='cards/c3.md', cardSha=hashlib.sha256(card).hexdigest(),
                    expectedTargetHash=None, sourceFingerprints=observed, sourceSpans=[],
                    verifiedMode='cross-engine', verifyEngine='claude', producingEngine='synthetic:codex')
        vd, rd = values.provenance_digests({'verdict': 'pass', 'claims': [], 'reasons': []}, data)
        intent = dict(kind='compile', **data, verdict_digest=vd, result_digest=rd)
        directory = self.case.state / 'provenance-intents'
        directory.mkdir(mode=0o700)
        identity = wiki_verify_worker._compile_intent_id(data)
        path = directory / ('compile-' + identity + '.json')
        ledger._write(path, intent, replace=False)
        with closing(sqlite3.connect(self.store_path)) as db:
            before = db.execute("SELECT COUNT(*) FROM card_attestations WHERE verification_kind='compiled'").fetchone()[0]
        result, _ = self.run_pass()
        self.assertEqual(result.get('provenance', {}).get('consumed'), 1)
        self.assertTrue(path.with_suffix('.consumed.json').is_file())
        self.assertEqual(self.head(), head)
        self.assertEqual(self.member_trust(head[0], 'cards/c3.md'), trust)
        result, _ = self.run_pass()
        self.assertEqual(result.get('provenance', {}).get('consumed', 0), 0)
        with closing(sqlite3.connect(self.store_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_attestations WHERE verification_kind='compiled'").fetchone()[0], before + 1)


class GenerationSync(Contract, rows.publish.Schema3SyncPublishChecks):
    pass


class RowSync(Contract, rows.RowSyncPublishChecks):
    pass


if __name__ == '__main__':
    unittest.main()
