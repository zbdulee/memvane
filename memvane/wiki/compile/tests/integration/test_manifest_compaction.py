"""Manifest compaction keeps the newest equivalent row for deletion detection."""
import json
import random

from memvane.wiki.compile import wiki_compile as compiler
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase


class ManifestCompaction(FixtureCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.tmp / 'manifest.jsonl'

    def write_rows(self, rows):
        self.manifest.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def rows(self):
        return [json.loads(line) for line in self.manifest.read_text().splitlines()]

    def test_keeps_latest_identity_and_preserves_unrelated_rows(self):
        self.write_rows([{'targetPath': 'a.md', 'canonicalKey': 'a', 'status': 'generated'},
                         {'targetPath': 'b.md', 'canonicalKey': 'b', 'status': 'generated'},
                         {'targetPath': 'a.md', 'canonicalKey': 'a', 'status': 'verified'}])
        workspace.compact_manifest(self.manifest, max_bytes=1)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(row for row in rows if row['targetPath'] == 'a.md')['status'], 'verified')
        self.assertEqual({row['targetPath'] for row in rows}, {'a.md', 'b.md'})

    def test_reverse_scan_matches_pairwise_identity_oracle(self):
        randomizer = random.Random(20260714)
        stamp = self.manifest.with_suffix('.jsonl.compact-stamp')
        for trial in range(200):
            rows = [{'id': i, 'targetPath': randomizer.choice([None, '', 'a.md', 'b.md', 'c.md']),
                     'sourceHash': randomizer.choice([None, '', 'h1', 'h2']),
                     'canonicalKey': randomizer.choice([None, '', 'k1', 'k2']),
                     'targetResolution': randomizer.choice([None, 'explicit', 'inferred'])}
                    for i in range(randomizer.randint(1, 12))]
            expected = [row['id'] for i, row in enumerate(rows)
                        if not any(compiler.same_generated_identity(row, later) or
                                   compiler.same_generated_identity(later, row) for later in rows[i + 1:])]
            self.write_rows(rows)
            stamp.unlink(missing_ok=True)
            workspace.compact_manifest(self.manifest, max_bytes=0)
            self.assertEqual([row['id'] for row in self.rows()], expected, 'trial %d' % trial)

    def test_stamp_avoids_reprocessing_until_the_manifest_grows(self):
        rows = [{'targetPath': 'a.md'}, {'targetPath': 'b.md'}]
        self.write_rows(rows)
        workspace.compact_manifest(self.manifest, max_bytes=1)
        stamp = self.manifest.with_suffix('.jsonl.compact-stamp')
        initial = stamp.stat().st_mtime_ns
        initial_size = int(stamp.read_text())
        workspace.compact_manifest(self.manifest, max_bytes=1)
        self.assertEqual(stamp.stat().st_mtime_ns, initial)
        self.write_rows(rows + [{'targetPath': 'large.md', 'padding': 'x' * 1000}])
        workspace.compact_manifest(self.manifest, max_bytes=1)
        self.assertGreater(int(stamp.read_text()), initial_size)
        self.assertEqual(int(stamp.read_text()), self.manifest.stat().st_size)
        self.assertEqual(self.rows()[-1]['targetPath'], 'large.md')
