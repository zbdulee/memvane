"""A direct writer reaches the interrupted visible deletion before the verifier retries."""
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import patch

from memvane.kernel import paths
from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.compile.tests.integration import test_pending_handoff as cases
from memvane.wiki.engines import api as engines
from memvane.wiki.verify import api as verifier
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase


class DirectWriter(FixtureCase):
    prepare = cases.PendingHandoff.prepare
    native_verify = cases.PendingHandoff.native_verify

    def setUp(self):
        super().setUp()
        self.prepare('decision')
        self.bound = None
        self.target.unlink()

    def candidate(self, text):
        if self.source.read_text() != text:
            self.source.write_text(text)
        revision = self.project.revision(self.source)
        return {'title': 'Decision', 'summary': 'Decision in the current source.', 'engine': 'claude',
                'trigger': 'post_tool_source', 'targetPath': self.target.relative_to(self.root).as_posix(),
                'sourceRevisions': [revision], 'sources': [{k: revision[k] for k in ('kind', 'path', 'collection')}]}

    def write(self, text, *, check=False):
        candidate = self.candidate(text)
        with workspace.card_write_lock(self.root), workspace.mutation_lock(self.root), \
             patch.object(workspace, 'selected', return_value=None), redirect_stdout(io.StringIO()) as output:
            rc = writer._compile_locked(self.root, self.config, self.config['compile'],
                                        self.project.wiki, candidate, False, check=check)
        return rc, json.loads(output.getvalue())['action']

    def rows(self, name):
        path = self.queue.with_name(name)
        return [json.loads(line) for line in path.read_bytes().split(b'\n') if line] if path.exists() else []

    def interrupt_native(self, *, duplicate=False):
        self.assertEqual(self.write('Newest source for decision.'), (0, 'created'))
        if duplicate:
            manifest = self.queue.with_name('generated-manifest.jsonl')
            manifest.write_bytes(manifest.read_bytes() * 2)
        child = os.fork()
        if child == 0:
            try:
                fail = {'verdict': 'fail', 'claims': [{'claim': 'Decision', 'supported': False, 'quote': ''}],
                        'reasons': ['source contradicts claim']}
                open_path = Path.open
                def interrupted(path, mode='r', *args, **kwargs):
                    if path.name == 'generated-manifest.jsonl' and mode == 'a':
                        os._exit(27)
                    return open_path(path, mode, *args, **kwargs)
                with patch.object(engines, 'run_extractor', return_value=(fail, None, 0)), \
                     patch.object(Path, 'open', interrupted):
                    verifier.run(self.root, self.config, self.config['compile'])
            finally:
                os._exit(28)
        self.assertEqual(os.waitpid(child, 0)[1], 27 << 8)
        self.assertFalse(self.target.exists())
        self.assertEqual(len(self.rows('verify-deleted.jsonl')), 1)

    def test_direct_writer_recovers_once_then_respects_a_manual_delete_of_the_new_generation(self):
        self.interrupt_native()
        corrected = 'Newest corrected source after machine removal.'
        self.assertEqual(self.write(corrected, check=True), (0, 'written'))
        self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')], ['created'])
        self.assertFalse(list(self.queue.parent.glob('retired-wiki/.promotions/*/finished')))
        self.assertEqual(self.write(corrected), (0, 'created'))
        for _ in range(2):
            self.assertEqual(self.native_verify()['remaining'], 0)
        self.assertTrue(self.target.is_file())
        self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')],
                         ['created', 'verify-deleted', 'created'])
        self.assertEqual(len(self.rows('verify-deleted.jsonl')), 1)
        self.assertEqual(self.rows('tombstones.jsonl'), [])
        self.target.unlink()
        self.assertEqual(self.write(corrected), (0, 'tombstoned'))
        self.assertEqual(self.write(corrected), (0, 'suppressed'))

    def test_interrupted_retirement_matches_archived_inode_across_apfs_device_change(self):
        self.interrupt_native()
        folder, = self.queue.parent.glob('retired-wiki/.promotions/*/retire')
        folder = folder.parent
        for name, field in (('retire', 'reviewed'), ('archive.json', 'displaced')):
            path = folder / name
            data = json.loads(path.read_bytes())
            data.pop('checksum')
            data[field]['dev'] += 999
            checksum = hashlib.sha256(canonical_json_bytes(data)).hexdigest()
            path.write_bytes(canonical_json_bytes(dict(data, checksum=checksum)))
        self.assertEqual(self.write('Newest corrected source after reboot.', check=True), (0, 'written'))
        self.assertEqual(self.write('Newest corrected source after reboot.'), (0, 'created'))
        self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')],
                         ['created', 'verify-deleted', 'created'])
        self.assertEqual(len(self.rows('verify-deleted.jsonl')), 1)
        self.assertEqual(self.rows('tombstones.jsonl'), [])

    def append_hidden_audits(self):
        target = self.target.relative_to(self.root).as_posix()
        rows = [{'targetPath': target, 'verdict': 'stale_replacement'},
                {'targetPath': target + '.pending-' + 'a' * 16, 'verdict': 'inconclusive'}]
        with self.queue.with_name('verify-deleted.jsonl').open('a') as stream:
            for row in rows:
                stream.write(json.dumps(row) + '\n')

    def test_hidden_deletion_audits_preserve_a_manual_removal(self):
        self.assertEqual(self.write('Newest source.'), (0, 'created'))
        self.append_hidden_audits()
        self.target.unlink()
        self.assertEqual(self.write('Newest corrected source.'), (0, 'tombstoned'))

    def test_hidden_deletion_audits_do_not_replace_the_interrupted_visible_deletion(self):
        self.interrupt_native()
        self.append_hidden_audits()
        self.assertEqual(self.write('Newest corrected source.'), (0, 'created'))
        self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')],
                         ['created', 'verify-deleted', 'created'])
        self.assertEqual(self.rows('tombstones.jsonl'), [])

    def test_compaction_without_generation_proof_uses_the_manual_delete_rule(self):
        self.interrupt_native(duplicate=True)
        manifest = self.queue.with_name('generated-manifest.jsonl')
        workspace.compact_manifest(manifest, max_bytes=0)
        self.assertEqual([r['action'] for r in self.rows(manifest.name)], ['created'])
        self.assertEqual(self.write('Newest corrected source.'), (0, 'tombstoned'))
        self.assertEqual(self.write('Newest source changed again.'), (0, 'suppressed'))
        self.assertEqual(len(self.rows('tombstones.jsonl')), 1)
        self.assertEqual([r['action'] for r in self.rows(manifest.name)], ['created'])

    def test_changed_archive_content_cannot_authorize_a_machine_delete(self):
        self.interrupt_native()
        slot, = self.queue.parent.glob('retired-wiki/.promotions/*/displaced.md')
        changed = slot.read_bytes() + b'editor append'
        slot.write_bytes(changed)
        self.assertEqual(self.write('Newest corrected source.'), (0, 'tombstoned'))
        self.assertEqual(self.write('Newest source changed again.'), (0, 'suppressed'))
        self.assertEqual(len(self.rows('tombstones.jsonl')), 1)
        self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')], ['created'])
        self.assertEqual(slot.read_bytes(), changed)

    def test_different_archive_inode_cannot_authorize_a_machine_delete(self):
        self.interrupt_native()
        slot, = self.queue.parent.glob('retired-wiki/.promotions/*/displaced.md')
        replacement = slot.with_name('replacement')
        replacement.write_bytes(slot.read_bytes())
        replacement.replace(slot)
        self.assertEqual(self.write('Newest corrected source.'), (0, 'tombstoned'))
        self.assertEqual(len(self.rows('tombstones.jsonl')), 1)
        self.assertFalse(self.target.exists())

    def interrupt_tag(self, revision, cut='before_manifest'):
        repo = Path.cwd()
        tag = cases.archived_tag(repo, self.tmp, revision)
        script = Path(__file__).resolve().parents[1] / 'tagged_retirement.py'
        r = subprocess.run([sys.executable, '-B', str(script), str(self.root), cut], cwd=tag, capture_output=True,
                           text=True, timeout=20, input=json.dumps([self.config, self.candidate('Newest source.')]),
                           env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(tag), str(repo)))})
        self.assertEqual(r.returncode, 27, r.stderr)
        self.assertEqual(len(self.rows('verify-deleted.jsonl')), 1)
        self.assertFalse(self.target.exists())

    def test_updated_generation_after_native_or_legacy_retirement_keeps_manual_delete(self):
        for revision in (None, '01136dc964e42c2c7dca2f27951612dc3cdc3dee'):
            with self.subTest(revision=revision):
                self.prepare('updated-' + str(revision))
                self.bound = None
                self.target.unlink()
                if revision is None:
                    self.assertEqual(self.write('Newest initial source.'), (0, 'created'))
                    self.assertEqual(self.negative_verify('fail')['remaining'], 0)
                else:
                    self.interrupt_tag(revision, 'before_finished')
                    self.assertFalse(list(self.queue.parent.glob('retired-wiki/.promotions/*/finished')))
                self.assertFalse(self.target.exists())
                with patch.object(writer, 'now_iso', return_value='2099-01-01T00:00:01Z'):
                    self.assertEqual(self.write('Newest recreated source.'), (0, 'created'))
                self.assertEqual(self.native_verify(), {'calls': 1, 'remaining': 0})
                with patch.object(writer, 'now_iso', return_value='2099-01-01T00:00:05Z'):
                    self.assertEqual(self.write('Newest edited source.'), (0, 'updated'))
                self.assertEqual(self.native_verify(), {'calls': 1, 'remaining': 0})
                self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')],
                                 ['created', 'verify-deleted', 'created', 'updated'])
                self.assert_manual_delete()
                self.assertEqual(len(self.rows('generated-manifest.jsonl')), 4)

    def test_first_native_format_without_file_generation_proof_uses_tombstone(self):
        self.interrupt_tag('01136dc964e42c2c7dca2f27951612dc3cdc3dee')
        archive, = self.queue.parent.glob('retired-wiki/.promotions/*/displaced.md')
        before = archive.read_bytes()
        self.assertEqual(self.write('Newest corrected source.'), (0, 'tombstoned'))
        self.assertEqual(self.write('Newest source changed again.'), (0, 'suppressed'))
        self.assertEqual(len(self.rows('tombstones.jsonl')), 1)
        self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')], ['created'])
        self.assertEqual(archive.read_bytes(), before)

    def test_byte_identical_same_second_creation_survives_compaction_as_new_history(self):
        manifest = self.queue.with_name('generated-manifest.jsonl')
        source = 'Newest source used for both generations.'
        stamp = '2026-11-24T01:00:00Z'
        with patch.object(writer, 'now_iso', return_value=stamp), patch.object(paths, 'datetime') as clock:
            clock.now.return_value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            self.assertEqual(self.write(source), (0, 'created'))
            created, = self.rows(manifest.name)

            open_file = os.open
            def stop_at_completion(path, *args, **kwargs):
                if isinstance(path, Path) and path.name == '.finished.partial':
                    raise KeyboardInterrupt
                return open_file(path, *args, **kwargs)

            with patch.object(os, 'open', side_effect=stop_at_completion), self.assertRaises(KeyboardInterrupt):
                self.negative_verify('fail')
            self.assertFalse(self.target.exists())
            self.assertFalse(list(self.queue.parent.glob('retired-wiki/.promotions/*/finished')))
            before, marker = self.rows(manifest.name)
            self.assertEqual((before, marker['action'], marker['ts']), (created, 'verify-deleted', stamp))
            self.assertEqual(self.write(source), (0, 'created'))
            self.assertEqual(self.rows(manifest.name), [created, marker, created])
        workspace.compact_manifest(manifest, max_bytes=0)
        self.assertEqual(self.rows(manifest.name), [created])
        self.assert_manual_delete()
        self.assertEqual(self.rows(manifest.name), [created])

    def negative_verify(self, verdict, *, suppression_failure=False):
        answer = {'verdict': verdict, 'claims': [{'claim': 'Decision', 'supported': False, 'quote': ''}],
                  'reasons': ['source contradicts claim']}
        open_path = Path.open

        def open_guard(path, mode='r', *args, **kwargs):
            if suppression_failure and path.name == 'verify-skipped.jsonl' and mode == 'a':
                raise OSError(errno.ENOSPC, 'fixture disk full')
            return open_path(path, mode, *args, **kwargs)

        with patch.object(workspace, 'selected', return_value=None), \
             patch.object(workspace, 'request_sync'), patch.object(Path, 'open', open_guard), \
             patch.object(engines, 'run_extractor', return_value=(answer, None, 0)) as calls:
            result = verifier.run(self.root, self.config, self.config['compile'])
        self.assertEqual(calls.call_count, 1)
        return result

    def assert_manual_delete(self):
        self.target.unlink()
        self.assertEqual(self.write('Newest corrected source.'), (0, 'tombstoned'))
        self.assertEqual(self.write('Newest source changed again.'), (0, 'suppressed'))
        tombstone, = self.rows('tombstones.jsonl')
        self.assertEqual(tombstone['targetPath'], self.target.relative_to(self.root).as_posix())
        self.assertFalse(self.target.exists())

    def test_real_pending_only_negative_audit_preserves_the_later_manual_delete(self):
        for verdict in ('fail', 'inconclusive'):
            with self.subTest(verdict=verdict):
                self.prepare('pending-' + verdict)
                self.bound = None
                self.target.unlink()
                self.assertEqual(self.write('Newest source.'), (0, 'created'))
                self.assertEqual(self.native_verify(), {'calls': 1, 'remaining': 0})
                previous = self.target.read_bytes()
                self.assertEqual(self.write('Newest source edited.'), (0, 'updated'))
                self.assertEqual(self.negative_verify(verdict)['remaining'], 0)
                audit, = self.rows('verify-deleted.jsonl')
                self.assertEqual((audit['targetPath'], audit['verdict']),
                                 (self.target.relative_to(self.root).as_posix(), verdict))
                self.assertEqual(self.target.read_bytes(), previous)
                self.assertFalse(list(self.queue.parent.glob('retired-wiki/.promotions/*/retire')))
                self.assert_manual_delete()

    def test_failed_suppression_audit_alone_preserves_the_later_manual_delete(self):
        self.assertEqual(self.write('Newest source.'), (0, 'created'))
        before = self.target.read_bytes()
        self.negative_verify('inconclusive', suppression_failure=True)
        audit, = self.rows('verify-deleted.jsonl')
        self.assertEqual(audit['verdict'], 'inconclusive')
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.rows('verify-skipped.jsonl'), [])
        self.assertFalse(list(self.queue.parent.glob('retired-wiki/.promotions/*/retire')))
        self.assert_manual_delete()

    def test_released_audit_only_preserves_the_manual_deletion_rule(self):
        self.interrupt_tag('v1.1.0')
        self.assertEqual(self.write('Newest corrected source.', check=True), (0, 'tombstoned'))
        self.assertEqual(self.rows('tombstones.jsonl'), [])
        self.assertEqual(self.write('Newest corrected source.'), (0, 'tombstoned'))
        self.assertEqual(self.write('Newest source changed again.'), (0, 'suppressed'))
        self.assertEqual(len(self.rows('tombstones.jsonl')), 1)
        self.assertEqual([r['action'] for r in self.rows('generated-manifest.jsonl')], ['created'])
        self.assertFalse(self.target.exists())
