"""The compatibility projection resumes on physical LF boundaries."""
import json
import sqlite3
from unittest.mock import patch

from memvane.wiki.card import legacy_ledgers, manifest_scan
from tests.support.case import FixtureCase


class ManifestCheckpoint(FixtureCase):
    def manifest(self):
        path = self.tmp / 'generated-manifest.jsonl'
        row = {'targetPath': 'wiki/seed.md', 'action': 'created', 'sourceHash': 'seed'}
        return path, (json.dumps(row) + '\n').encode()

    def test_batch_runs_outside_sqlite_write_lock_and_retries_torn_tail(self):
        path, row = self.manifest()
        path.write_bytes(row * 4096 + row[:-5])
        self.assertFalse(legacy_ledgers.writable(path.parent))
        self.assertFalse(legacy_ledgers.writable(path.parent))
        with path.open('ab') as stream:
            stream.write(row[-5:])
        db_path = path.parent / '.handoff/v1/manifest-scan/projection.db'
        read_batch = legacy_ledgers._manifest_batch
        starts = []

        def observe(*args):
            starts.append(args[2])
            with sqlite3.connect(str(db_path), timeout=0) as db:
                db.execute('BEGIN IMMEDIATE')
                return read_batch(*args)

        with patch.object(legacy_ledgers, '_manifest_batch', side_effect=observe):
            self.assertTrue(legacy_ledgers.writable(path.parent))
        self.assertEqual(starts, [4096 * len(row)])

    def test_projection_limit_counts_distinct_encoded_rows_and_rolls_back(self):
        path, row = self.manifest()
        template = json.loads(row)
        records = [dict(template, sourceHash=str(n), targetPath='wiki/' + str(n))
                   for n in range(4097)]
        original = b''.join((json.dumps(record) + '\n').encode() for record in records)
        path.write_bytes(original)
        limit = sum(len(json.dumps(legacy_ledgers._manifest_projection(record))) for record in records[:-1])
        with patch.object(manifest_scan, 'LIMIT', limit):
            self.assertFalse(legacy_ledgers.writable(path.parent))
            self.assertFalse(legacy_ledgers.writable(path.parent))
        db_path = path.parent / '.handoff/v1/manifest-scan/projection.db'
        with sqlite3.connect(str(db_path)) as db:
            size, count = db.execute('SELECT sum(length(CAST(row AS BLOB))), count(*) FROM projection').fetchone()
            self.assertEqual((size, count), (limit, 4096))
        self.assertEqual(path.read_bytes(), original)
