"""Released manifest rows spanning multiple passes can be compacted after validation."""
import json
from pathlib import Path
import subprocess
import sys

from memvane.wiki.card import api as wiki_freshness
from memvane.wiki.compile.tests import legacy_created
from memvane.wiki.compile.tests.integration import test_pending_handoff as handoff_cases
from memvane.wiki.workspace import api as manifest_ledger
from tests.support.case import FixtureCase


class ManifestScan(FixtureCase):
    prepare = handoff_cases.PendingHandoff.prepare
    write = handoff_cases.PendingHandoff.write

    def released_manifest(self, name):
        self.prepare(name)
        self.target.unlink()
        path = self.queue.with_name('generated-manifest.jsonl')
        path.parent.mkdir(exist_ok=True)
        return path, legacy_created.old_created(self, '-')

    def compact_in_new_process(self, path):
        subprocess.run([sys.executable, '-B', '-c',
                        'from pathlib import Path; import sys; '
                        'from memvane.wiki.workspace.api import compact_manifest; '
                        'compact_manifest(Path(sys.argv[1]), max_bytes=0)', str(path)],
                       cwd=Path.cwd(), check=True, timeout=20)

    def test_released_manifest_at_4096_and_4097_rows_allows_refresh_creation_and_compaction(self):
        for n in (4096, 4097):
            with self.subTest(n=n):
                path, row = self.released_manifest('large' + str(n))
                original = row * n
                path.write_bytes(original)
                self.assertLess(len(original), 8 * 1024 ** 2)
                event = wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude')
                self.assertEqual(bool(event), n == 4096)
                self.assertEqual(path.read_bytes(), original)
                for _ in range(2):
                    self.assertEqual(self.write('Newest source for unrelated review.'), 0)
                    manifest_ledger.compact_manifest(path, max_bytes=0)
                if not event:
                    self.assertTrue(wiki_freshness.record_pending_refresh(self.root, 'docs/source.md', 'claude'))
                self.assertTrue(self.target.is_file())
                targets = [json.loads(line)['targetPath'] for line in path.read_bytes().splitlines()]
                self.assertCountEqual(targets, [json.loads(row)['targetPath'],
                                                self.target.relative_to(self.root).as_posix()])

    def test_byte_limited_pass_resumes_in_another_process_and_restarts_on_replacement(self):
        path, released = self.released_manifest('byte-window')
        row = json.loads(released)
        row['summary'] = 'x' * 6000
        physical = (json.dumps(row) + '\n').encode()
        original = physical * ((8 * 1024 ** 2 // len(physical)) + 1)
        self.assertLess(original.count(b'\n'), 4096)
        path.write_bytes(original)
        self.compact_in_new_process(path)
        self.assertEqual(path.read_bytes(), original)
        # The inspected prefix belonged to the previous inode. A protected row in the
        # replacement's prefix must be noticed even if the public size is unchanged.
        protected = original.replace(b'xxxxxx', b'\\u2028', 1)
        replacement = path.with_suffix('.swap')
        replacement.write_bytes(protected)
        replacement.replace(path)
        for _ in range(2):
            self.compact_in_new_process(path)
            self.assertEqual(path.read_bytes(), protected)
        path.write_bytes(original)
        self.compact_in_new_process(path)
        self.assertEqual(path.read_bytes(), original)
        self.compact_in_new_process(path)
        self.assertEqual([json.loads(line) for line in path.read_bytes().splitlines()], [row])
