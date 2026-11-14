"""Actual sync owner consumes compiled evidence on an unchanged view."""
from contextlib import closing
import hashlib
import copy
import importlib.util
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('row_sync', HERE / 'laya-s-sync-rows-checks.py')
rows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rows)


class Contract:
    def prepare_intent(self):
        from memvane.store.blob import blob_values as values
        from memvane.install import ledger_common as ledger
        from memvane.wiki.verify import wiki_verify_worker
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
        return head, trust, data, intent, path, before

    def test_unchanged_pass_consumes_intent_once(self):
        head, trust, data, intent, path, before = self.prepare_intent()
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        held = path.with_suffix('.held')
        path.rename(held)
        with mock.patch.object(self.worker, '_mark_schema3_write', wraps=self.worker._mark_schema3_write) as marker:
            self.run_pass()
            self.assertEqual(marker.call_count, 0)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        held.rename(path)
        with mock.patch.object(self.worker, '_mark_schema3_write', wraps=self.worker._mark_schema3_write) as marker:
            result, _ = self.run_pass()
            self.assertEqual(marker.call_count, 1)
        self.assertEqual(result.get('provenance', {}).get('consumed'), 1, result)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch + 1)
        self.assertTrue(path.with_suffix('.consumed.json').is_file())
        self.assertEqual(self.head(), head)
        self.assertEqual(self.member_trust(head[0], 'cards/c3.md'), trust)
        result, _ = self.run_pass()
        self.assertEqual(result.get('provenance', {}).get('consumed', 0), 0)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch + 1)
        with closing(sqlite3.connect(self.store_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_attestations WHERE verification_kind='compiled'").fetchone()[0], before + 1)

    def test_tampered_parity_is_rejected_without_store_write(self):
        from memvane.install import ledger_common as ledger
        head, trust, data, intent, path, before = self.prepare_intent()
        intent['sourceFingerprints']['fingerprints'][0]['source_sha'] = 'b' * 64
        ledger._write(path, intent, replace=True)
        result, _ = self.run_pass()
        self.assertEqual(result['provenance']['rejected'].get('provenance_parity_mismatch'), 1)
        self.assertTrue(path.with_suffix('.rejected.json').is_file())
        self.assertEqual(self.head(), head)
        with closing(sqlite3.connect(self.store_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_attestations WHERE verification_kind='compiled'").fetchone()[0], before)

    def test_intent_with_absent_card_waits_without_write(self):
        from memvane.install import ledger_common as ledger
        from memvane.wiki.verify import wiki_verify_worker
        head, trust, data, intent, path, before = self.prepare_intent()
        path.unlink()
        intent['cardSha'] = 'd' * 64
        new_path = path.parent / ('compile-' + wiki_verify_worker._compile_intent_id(intent) + '.json')
        ledger._write(new_path, intent, replace=False)
        result, _ = self.run_pass()
        self.assertEqual(result['provenance']['waiting'], 1)
        self.assertTrue(new_path.is_file())
        self.assertEqual(self.head(), head)
        with closing(sqlite3.connect(self.store_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_attestations WHERE verification_kind='compiled'").fetchone()[0], before)

    def test_newer_consumed_supersedes_older_waiting(self):
        from memvane.install import ledger_common as ledger
        from memvane.store.blob import blob_values as values
        from memvane.wiki.verify import wiki_verify_worker
        head, trust, data, current, path, before = self.prepare_intent()
        old = copy.deepcopy(current)
        old['cardSha'] = 'd' * 64
        old['sourceFingerprints']['scanId'] = '3' * 32
        old['sourceFingerprints']['observedNs'] = 3
        old['sourceFingerprints']['fingerprints'][0]['scan_id'] = '3' * 32
        old['sourceFingerprints']['fingerprints'][0]['observed_ns'] = 3
        old['result_digest'] = values.compile_result_digest({k:v for k,v in old.items() if k not in ('kind','verdict_digest','result_digest')}, old['verdict_digest'])
        old_path = path.parent / ('compile-' + wiki_verify_worker._compile_intent_id(old) + '.json')
        ledger._write(old_path, old, replace=False)
        self.run_pass()
        self.assertTrue(path.with_suffix('.consumed.json').is_file())
        result, _ = self.run_pass()
        self.assertTrue(old_path.with_suffix('.superseded.json').is_file())
        self.assertFalse(old_path.exists())

    def test_waiting_queue_rotates_to_valid_intent(self):
        from memvane.install import ledger_common as ledger
        from memvane.wiki.verify import wiki_verify_worker
        head, trust, data, current, path, before = self.prepare_intent()
        n = 0
        added = 0
        while added < 600:
            n += 1
            waiting = copy.deepcopy(current)
            waiting['cardSha'] = hashlib.sha256(str(n).encode()).hexdigest()
            name = 'compile-' + wiki_verify_worker._compile_intent_id(waiting) + '.json'
            if name >= path.name:
                continue
            p = path.parent / name
            p.write_bytes(ledger._canon(waiting)); p.chmod(0o600)
            added += 1
        consumed = 0
        for _ in range(3):
            result, _ = self.run_pass()
            consumed += result['provenance']['consumed']
            if consumed:
                break
        self.assertEqual(consumed, 1)
        self.assertTrue(path.with_suffix('.consumed.json').is_file())

    def test_malformed_intent_does_not_block_valid_intent(self):
        head, trust, data, intent, path, before = self.prepare_intent()
        broken = path.parent / ('compile-' + '0' * 64 + '.json')
        broken.write_bytes(b'{invalid'); broken.chmod(0o600)
        result, _ = self.run_pass()
        self.assertEqual(result['provenance']['consumed'], 1, result)
        self.assertTrue(path.with_suffix('.consumed.json').exists())
        self.assertTrue(broken.exists())

    def test_forged_consumed_marker_cannot_supersede_waiting(self):
        from memvane.install import ledger_common as ledger
        from memvane.store.blob import blob_values as values
        from memvane.wiki.verify import wiki_verify_worker
        head, trust, data, current, path, before = self.prepare_intent()
        path.rename(path.with_suffix('.consumed.json'))
        old = copy.deepcopy(current)
        old['cardSha'] = 'd' * 64
        old['sourceFingerprints']['scanId'] = '3' * 32
        old['sourceFingerprints']['observedNs'] = 3
        old['sourceFingerprints']['fingerprints'][0]['scan_id'] = '3' * 32
        old['sourceFingerprints']['fingerprints'][0]['observed_ns'] = 3
        old['result_digest'] = values.compile_result_digest({k:v for k,v in old.items() if k not in ('kind','verdict_digest','result_digest')}, old['verdict_digest'])
        old_path = path.parent / ('compile-' + wiki_verify_worker._compile_intent_id(old) + '.json')
        ledger._write(old_path, old, replace=False)
        result, _ = self.run_pass()
        self.assertEqual(result['provenance']['superseded'], 0)
        self.assertTrue(old_path.exists())

    def test_swapped_intent_is_never_acknowledged(self):
        from memvane.install import ledger_common as ledger
        from memvane.kernel.errors import IndexContractError
        head, trust, data, intent, path, before = self.prepare_intent()
        real = self.views.ViewStore.put_compiled_attestation
        def swap(store, *args, **kwargs):
            answer = real(store, *args, **kwargs)
            ledger._write(path, intent, replace=True)
            return answer
        with mock.patch.object(self.views.ViewStore, 'put_compiled_attestation', new=swap):
            with self.assertRaisesRegex(IndexContractError, 'provenance_intent_changed'):
                self.run_pass()
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.consumed.json').exists())


class GenerationSync(Contract, rows.publish.Schema3SyncPublishChecks):
    pass


class RowSync(Contract, rows.RowSyncPublishChecks):
    pass


if __name__ == '__main__':
    unittest.main()
