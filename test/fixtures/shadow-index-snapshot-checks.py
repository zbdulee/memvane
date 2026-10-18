#!/usr/bin/env python3
"""C2a disposable SQLite/WAL backup and stale publication race.

There is no QMD executable, model, installed store, or operating pointer here.
"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import published_view
import sqlite_read


def view(revision):
    return {'owner': 'fixture-owner', 'incarnation': 'fixture-registration',
            'worktree': 'fixture-worktree', 'scopeRevision': 'fixture-settings',
            'storeGeneration': 'fixture-store', 'contentRevision': revision}


def active_text(path):
    with sqlite3.connect(path) as db:
        return db.execute('SELECT body FROM documents WHERE name=?', ('story',)).fetchone()[0]


class ShadowSnapshot(unittest.TestCase):
    def test_live_wal_backup_keeps_old_view_and_rejects_late_worker(self):
        with tempfile.TemporaryDirectory(prefix='qmd-c2a-shadow-') as directory:
            base = Path(directory)
            source, candidate, pointer = (base / name for name in
                                          ('source.sqlite', 'candidate.sqlite', 'active.json'))
            writer = sqlite3.connect(source)
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE documents(name TEXT PRIMARY KEY, body TEXT)')
                writer.execute('INSERT INTO documents VALUES (?,?)', ('story', 'OLD'))
                writer.commit()
                pointer.write_text(json.dumps(view('old')))
                self.assertTrue(Path(str(source) + '-wal').exists())

                # SQLite backup copies committed WAL pages without checkpointing
                # or modifying the live source. All paths are inside this tempdir.
                with sqlite_read.connect(source) as original:
                    with sqlite3.connect(candidate) as prepared:
                        original.backup(prepared)
                self.assertEqual(active_text(source), 'OLD')
                self.assertEqual(active_text(candidate), 'OLD')

                # Simulated QMD work is applied only to the inactive copy.
                with sqlite3.connect(candidate) as prepared:
                    prepared.execute('UPDATE documents SET body=? WHERE name=?',
                                     ('NEW', 'story'))
                self.assertEqual(active_text(source), 'OLD')
                self.assertEqual(active_text(candidate), 'NEW')

                expected = json.loads(pointer.read_text())
                published = published_view.publication_decision(
                    expected, expected, view('new'))
                self.assertEqual(published, view('new'))
                pointer.write_text(json.dumps(published))

                # An old worker can still finish writing its old source DB;
                # the synthetic CAS refuses its stale attempt to replace B.
                writer.execute('UPDATE documents SET body=? WHERE name=?',
                               ('OLD-WORKER-LATE', 'story'))
                writer.commit()
                current = json.loads(pointer.read_text())
                self.assertIsNone(published_view.publication_decision(
                    current, expected, view('stale')))
                self.assertEqual(json.loads(pointer.read_text()), view('new'))
                self.assertEqual(active_text(candidate), 'NEW')
            finally:
                writer.close()

    def test_failed_candidate_never_advances_pointer(self):
        with tempfile.TemporaryDirectory(prefix='qmd-c2a-failure-') as directory:
            base = Path(directory)
            source, candidate, pointer = (base / name for name in
                                          ('source.sqlite', 'candidate.sqlite', 'active.json'))
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE documents(body TEXT)')
                db.execute('INSERT INTO documents VALUES (?)', ('OLD',))
            pointer.write_text(json.dumps(view('old')))
            with self.assertRaises(sqlite3.DatabaseError):
                with sqlite3.connect(candidate) as db:
                    db.execute('INSERT INTO missing_table VALUES (?)', ('BAD',))
            self.assertEqual(json.loads(pointer.read_text()), view('old'))
            with sqlite3.connect(source) as db:
                self.assertEqual(db.execute('SELECT body FROM documents').fetchone()[0], 'OLD')


if __name__ == '__main__':
    unittest.main()
