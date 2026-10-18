#!/usr/bin/env python3
"""Disposable config + private publication composition; never launches QMD."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import shadow_qmd_adapter as adapter
import shadow_ack_policy as ack_policy
import shadow_index


class SimulatedCrash(BaseException):
    pass

TAIL = 'models:\n  embed: synthetic-model\n'
BASELINE_SHA = hashlib.sha256(('collections: {}\n' + TAIL).encode()).hexdigest()


def view(revision, generation):
    return {'owner': 'fixture-owner', 'incarnation': 'fixture-registration',
            'worktree': 'fixture-worktree', 'scopeRevision': 'fixture-scope',
            'storeGeneration': 'fixture-store', 'contentRevision': revision,
            'indexGeneration': generation}


class ShadowQmdAdapter(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qmd-private-compose-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite'
        self.collection = self.root / 'approved-source'
        self.collection.mkdir()
        self.approved = {'fixture-wiki': str(self.collection)}
        self.config = ('collections:\n  fixture-wiki:\n    path: '
                       + str(self.collection) + '\n    pattern: "**/*.md"\n'
                       + TAIL).encode()
        self.writer = sqlite3.connect(self.source)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('CREATE TABLE documents(body TEXT)')
        self.writer.execute('INSERT INTO documents VALUES (?)', ('old',))
        self.writer.execute('CREATE TABLE store_collections(name TEXT, path TEXT, pattern TEXT)')
        self.writer.execute('INSERT INTO store_collections VALUES (?,?,?)',
                            ('fixture-wiki', str(self.collection), '**/*.md'))
        self.writer.commit()
        self.old = view('old', 'g-' + '0' * 32)
        self.pointer = self.root / 'published-view.json'
        self.pointer.write_text(json.dumps(self.old))
        self.pointer.chmod(0o600)
        self.acks = []
        self.called = []

    def run_adapter(self, source=None, expected=None, revision='new', config=None,
                    worker=None, acknowledge=None, claim=None, retain_receipt=None):
        def normal(index, env):
            self.called.append((index, dict(env)))
            with sqlite3.connect(index) as db:
                db.execute("UPDATE documents SET body=body || '-new'")
        return adapter.run(self.root, source or self.source, expected or self.old,
                           revision, config_bytes=self.config if config is None else config,
                           baseline_sha256=BASELINE_SHA, approved=self.approved,
                           work_candidate=worker or normal,
                           acknowledge=acknowledge or self.acks.append,
                           claim=claim, retain_receipt=retain_receipt)

    def body(self, index):
        with sqlite3.connect(index) as db:
            return db.execute('SELECT body FROM documents').fetchone()[0]

    def test_proved_config_scope_and_candidate_only_work_before_ack(self):
        result = self.run_adapter()
        self.assertEqual((result['outcome'], result['ack']), ('published', 'acknowledged'))
        self.assertEqual(json.loads(self.pointer.read_text()), result['view'])
        self.assertEqual(self.acks, [result['view']])
        self.assertEqual(self.body(self.source), 'old')
        self.assertEqual(self.body(result['candidate']['index']), 'old-new')
        index, env = self.called[0]
        self.assertEqual(env['INDEX_PATH'], index)
        self.assertEqual(Path(env['QMD_CONFIG_DIR'], 'index.yml').read_bytes(), self.config)
        self.assertTrue(Path(env['XDG_CACHE_HOME']).is_dir())
        self.assertEqual(Path(env['QMD_CONFIG_DIR']).stat().st_mode & 0o077, 0)

    def test_shell_update_is_rejected_before_backup_or_worker(self):
        malicious = self.config.replace(b'    pattern: "**/*.md"\n',
                                        b'    pattern: "**/*.md"\n    update: echo bad\n')
        result = self.run_adapter(config=malicious)
        self.assertEqual((result['outcome'], result['reason']),
                         ('rejected', 'candidate_config_shell_update_command'))
        self.assertEqual(self.called, [])
        self.assertEqual(self.acks, [])
        self.assertFalse((self.root / 'candidates').exists())
        self.assertEqual(json.loads(self.pointer.read_text()), self.old)

    def test_physical_collection_mismatch_rejects_without_worker_or_ack(self):
        self.writer.execute('UPDATE store_collections SET path=?', ('/other',))
        self.writer.commit()
        result = self.run_adapter()
        self.assertEqual(result['outcome'], 'rejected')
        self.assertIn('candidate_collections_unproved', result['reason'])
        self.assertEqual(self.called, [])
        self.assertEqual(self.acks, [])
        self.assertEqual(json.loads(self.pointer.read_text()), self.old)

    def test_config_mutation_after_worker_is_rejected_without_ack(self):
        def mutate(index, env):
            self.called.append((index, dict(env)))
            Path(env['QMD_CONFIG_DIR'], 'index.yml').write_bytes(b'unsafe\n')
        result = self.run_adapter(worker=mutate)
        self.assertEqual(result['outcome'], 'rejected')
        self.assertIn('candidate_config_changed', result['reason'])
        self.assertEqual(self.acks, [])
        self.assertEqual(json.loads(self.pointer.read_text()), self.old)

    def test_second_publication_uses_first_candidate_as_source(self):
        first = self.run_adapter(revision='one')
        self.assertEqual(first['outcome'], 'published')
        wrong = self.run_adapter(expected=first['view'], revision='two')
        self.assertEqual((wrong['outcome'], wrong['reason']),
                         ('rejected', 'published_source_mismatch'))
        second = self.run_adapter(source=first['candidate']['index'],
                                  expected=first['view'], revision='two')
        self.assertEqual(second['outcome'], 'published')
        self.assertEqual(self.body(second['candidate']['index']), 'old-new-new')
        self.assertEqual(self.acks, [first['view'], second['view']])

    def test_receipt_retention_failure_prevents_private_cas_and_ack(self):
        claim = {'id': 'synthetic-claim', 'digest': 'a' * 64}
        def fail_retention(receipt):
            raise OSError('private receipt unavailable')
        result = self.run_adapter(claim=claim, retain_receipt=fail_retention)
        self.assertEqual(result['outcome'], 'rejected')
        self.assertIn('receipt_retention_failed', result['reason'])
        self.assertEqual(json.loads(self.pointer.read_text()), self.old)
        self.assertEqual(self.acks, [])

    def test_persisted_pre_cas_receipt_recovers_crash_before_ack(self):
        claim = {'id': 'synthetic-claim', 'digest': 'a' * 64}
        receipt_path = self.root / 'synthetic-receipt.json'
        def retain(receipt):
            with receipt_path.open('x') as output:
                json.dump(receipt, output)
                output.flush()
                os.fsync(output.fileno())
        def crash_after_cas(view):
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.run_adapter(claim=claim, retain_receipt=retain,
                             acknowledge=crash_after_cas)
        receipt = json.loads(receipt_path.read_text())
        self.assertTrue(ack_policy.valid_receipt(receipt))
        current = json.loads(self.pointer.read_text())
        self.assertEqual(current, receipt['prepared'])
        self.assertEqual(self.acks, [])
        seal = shadow_index.seal_candidate(
            self.root, receipt['prepared']['indexGeneration'])
        decision = ack_policy.decide(receipt, current, claim, seal)
        self.assertEqual((decision['decision'], decision['ack']), ('ack', claim))
        self.assertNotEqual(ack_policy.decide(
            receipt, current, {'id': 'other', 'digest': claim['digest']}, seal)['decision'], 'ack')
        self.assertNotEqual(ack_policy.decide(
            receipt, self.old, claim, seal)['decision'], 'ack')


if __name__ == '__main__':
    unittest.main()
