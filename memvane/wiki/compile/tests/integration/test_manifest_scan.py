"""Released manifest rows spanning multiple passes can be compacted after validation."""
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.card import api as wiki_freshness
from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.compile.tests import legacy_created
from memvane.wiki.compile.tests import release_tags
from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as manifest_ledger
from memvane.wiki.workspace import types as workspace_types
from tests.support.queue_files import pending_text
from tests.support.case import FixtureCase


class ManifestScan(FixtureCase):
    def prepare(self, name):
        release_tags.prepare_selected_card(self, name)

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

    def test_normal_writer_compacts_after_append_at_the_row_boundary(self):
        for n in (4096, 4097):
            with self.subTest(n=n):
                path, row = self.released_manifest('large' + str(n))
                path.write_bytes(row * n)
                self.assertGreater(path.stat().st_size, workspace_types.LOG_MAX_BYTES)
                for i in range(3):
                    result, action = self.compile('Newest source for unrelated review.', 'decision' + str(i))
                    # A first pass over an existing 4097-row file only advances the checkpoint.
                    self.assertEqual((result, action), (1, 'deferred') if n == 4097 and i == 0 else (0, 'created'))
                self.assertEqual(len(path.read_bytes().splitlines()), 4 if n == 4096 else 3)

    def compile(self, text, title, *, check=False):
        self.source.write_text(text)
        revision = self.project.revision(self.source)
        target = self.target.with_name(title + '.md')
        candidate = {'title': title, 'summary': title + ' in the current source.',
                     'engine': 'claude', 'trigger': 'post_tool_source',
                     'targetPath': target.relative_to(self.root).as_posix(),
                     'sourceRevisions': [revision],
                     'sources': [{k: revision[k] for k in ('kind', 'path', 'collection')}]}
        with patch.object(manifest_ledger, 'selected', return_value=None), redirect_stdout(io.StringIO()) as output:
            if check:
                rc = writer._compile_locked(self.root, self.config, self.config['compile'],
                                            self.project.wiki, candidate, False, check=True)
            else:
                with manifest_ledger.card_write_lock(self.root), manifest_ledger.mutation_lock(self.root):
                    rc = writer._compile_locked(self.root, self.config, self.config['compile'],
                                                self.project.wiki, candidate, False)
        return rc, json.loads(output.getvalue())['action']

    def test_first_post_tool_use_at_8193_rows_durably_queues_the_edit(self):
        path, row = self.released_manifest('large-hook')
        original = row * 8193
        path.write_bytes(original)
        payload = {'hook_event_name': 'PostToolUse', 'cwd': str(self.root), 'engine': 'claude',
                   'tool_input': {'file_path': str(self.source)}}
        with patch.object(manifest_ledger, 'request_compile') as wake, \
             patch.object(manifest_ledger, 'selected', return_value=None):
            result = intake.enqueue_payload(payload)
        self.assertEqual(result, {'queued': 1, 'reason': 'queued'})
        event, = wiki_freshness.unresolved_pending_refreshes_strict(self.root)
        queued, = (json.loads(r) for r in pending_text(path.with_name(workspace_types.SOURCE_QUEUE)).splitlines())
        self.assertEqual(queued['pendingRefresh']['eventId'], event['eventId'])
        self.assertEqual(event['sourcePath'], 'docs/source.md')
        self.assertEqual(wake.call_count, 1)
        self.assertEqual(path.read_bytes(), original)

    def test_compacted_manifest_needs_no_db_write_for_writable_or_check(self):
        path, row = self.released_manifest('read-only')
        path.write_bytes(row * 4097)
        self.assertIsNone(wiki_freshness.unresolved_pending_refreshes_strict(self.root, before_ledger_write=True))
        manifest_ledger.compact_manifest(path, max_bytes=0)
        self.assertEqual(path.read_bytes().count(b'\n'), 1)
        db_path = path.parent / '.handoff/v1/manifest-scan/projection.db'
        with path.open('ab') as stream:
            stream.write(row)
        with sqlite3.connect(str(db_path)) as db:
            db.execute('BEGIN IMMEDIATE')
            self.assertIsNotNone(wiki_freshness.unresolved_pending_refreshes_strict(self.root, before_ledger_write=True))
            db.rollback()
        original = db_path.read_bytes()
        db_path.chmod(0o400)
        db_path.parent.chmod(0o500)
        try:
            self.assertEqual(self.compile('Read-only check.', 'preview', check=True), (0, 'written'))
            self.assertEqual(db_path.read_bytes(), original)
            self.assertFalse(self.target.with_name('preview.md').exists())
        finally:
            db_path.parent.chmod(0o700)
            db_path.chmod(0o600)

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
