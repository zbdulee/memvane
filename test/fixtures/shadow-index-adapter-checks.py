#!/usr/bin/env python3
"""Disposable C2 adapter checks; no QMD binary, endpoint, model or user store."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import shadow_index


def view(revision, generation, **changes):
    row = {'owner': 'project-a', 'incarnation': 'registration-a',
           'worktree': 'worktree-a', 'scopeRevision': 'settings-a',
           'storeGeneration': 'store-a', 'contentRevision': revision,
           'indexGeneration': generation}
    row.update(changes)
    return row


class ShadowIndexAdapter(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qmd-shadow-adapter-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite'
        self.writer = sqlite3.connect(self.source)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('CREATE TABLE documents(name TEXT PRIMARY KEY, body TEXT)')
        self.writer.execute('INSERT INTO documents VALUES (?,?)', ('story', 'old'))
        self.writer.commit()
        self.old = view('old', 'g-' + '0' * 32)
        (self.root / 'published-view.json').write_text(json.dumps(self.old))
        (self.root / 'published-view.json').chmod(0o600)

    def candidate(self, body='new'):
        row = shadow_index.backup_inactive(self.source, self.root)
        with sqlite3.connect(row['index']) as db:
            self.assertEqual(db.execute('SELECT body FROM documents').fetchone()[0],
                             self.writer.execute('SELECT body FROM documents').fetchone()[0])
            db.execute('UPDATE documents SET body=?', (body,))
        sealed = shadow_index.seal_candidate(self.root, row['generation'])
        return row, sealed

    def pointer(self):
        return json.loads((self.root / 'published-view.json').read_text())

    def test_live_wal_backup_and_atomic_cas_reject_late_worker(self):
        first, proof = self.candidate()
        prepared = view('new', first['generation'])
        self.assertEqual(shadow_index.publish_candidate(
            self.root, self.old, prepared, proof), prepared)
        self.assertEqual(self.pointer(), prepared)
        self.writer.execute('UPDATE documents SET body=?', ('old-worker-late',))
        self.writer.commit()
        stale, stale_proof = self.candidate('stale')
        self.assertIsNone(shadow_index.publish_candidate(
            self.root, self.old, view('stale', stale['generation']), stale_proof))
        self.assertEqual(self.pointer(), prepared)
        with sqlite3.connect(first['index']) as db:
            self.assertEqual(db.execute('SELECT body FROM documents').fetchone()[0], 'new')

    def test_changed_candidate_or_owner_never_advances_pointer(self):
        candidate, proof = self.candidate()
        with sqlite3.connect(candidate['index']) as db:
            db.execute('UPDATE documents SET body=?', ('after-seal',))
        with self.assertRaisesRegex(ValueError, 'source_index_changed_during_migration'):
            shadow_index.publish_candidate(
                self.root, self.old, view('new', candidate['generation']), proof)
        self.assertEqual(self.pointer(), self.old)
        fresh = shadow_index.seal_candidate(self.root, candidate['generation'])
        with self.assertRaisesRegex(ValueError, 'invalid_shadow_index_view'):
            shadow_index.publish_candidate(self.root, self.old,
                view('new', self.old['indexGeneration']), fresh)
        self.assertIsNone(shadow_index.publish_candidate(self.root, self.old,
            view('new', candidate['generation'], incarnation='other'), fresh))
        self.assertEqual(self.pointer(), self.old)


if __name__ == '__main__':
    unittest.main()
