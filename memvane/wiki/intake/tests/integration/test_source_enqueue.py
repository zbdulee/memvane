"""Source queues preserve ordering, consent, caps and revision coalescing."""
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.intake import wiki_compile_enqueue as enqueue
from memvane.wiki.workspace import api as workspace
from memvane.wiki.workspace import types as workspace_types
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


def _crash_producer(queue, boundary, side):
    """Use the real producer; stop at a named syscall boundary in this fixture child."""
    module = enqueue.source_queue
    entry = {
        'reserve': (module.tempfile, 'NamedTemporaryFile'),
        'file_sync': (os, 'fsync'),
        'verify': (module, '_verify_image'),
        'rename': (os, 'replace'),
        'dir_sync': (module, 'fsync_dir'),
    }
    rows = [{'source': {'collection': 'docs', 'path': path}, 'contentRevision': 'new'}
            for path in ('docs/a.md', 'docs/b.md')]
    if boundary == 'replay':
        enqueue._append_jsonl(Path(queue), rows)
        return
    owner, name = entry[boundary]
    real = getattr(owner, name)
    def cut(*args, **kwargs):
        if side == 'before':
            os._exit(91)
        real(*args, **kwargs)
        os._exit(91)
    with patch.object(owner, name, side_effect=cut):
        enqueue._append_jsonl(Path(queue), rows)


class SourceEnqueue(FixtureCase):
    def setUp(self):
        super().setUp()
        limits = patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '64'})
        limits.start()
        self.addCleanup(limits.stop)
        self.project = make_project(self.home, git=False)
        for path in ('docs/a.md', 'docs/b.md', 'docs/c.md', 'src/s.md',
                     'docs/note.py', 'docs/.draft/hidden.md', 'wiki/w.md'):
            self.project.write_note(path, 'synthetic ' + path + '\n')
        self.config = expanded(self.project, compile={'mode': 'auto-wiki',
            'triggers': ['post_sync_source', 'session_start_source'],
            'extractor': {'builtins': ['claude']}})
        self.config.update(collections=['docs', 'src', 'wiki'],
                           collectionPaths={'docs': 'docs', 'src': 'src', 'wiki': 'wiki'},
                           collectionRoles={'docs': 'raw', 'src': 'source', 'wiki': 'wiki'})
        self.changed = {
            'docs': {'root': str(self.project.root / 'docs'), 'created': ['c.md', 'note.py', '.draft/hidden.md'],
                     'updated': ['b.md', 'a.md'], 'deleted': ['gone.md']},
            'src': {'root': str(self.project.root / 'src'), 'created': ['s.md'], 'updated': [], 'deleted': []},
            'wiki': {'root': str(self.project.root / 'wiki'), 'created': ['w.md'], 'updated': [], 'deleted': []}}
    @property
    def queue(self):
        return workspace.safe_queue_path(self.project.root, workspace.rel(workspace_types.SOURCE_QUEUE), native=True)

    def run_enqueue(self, **kwargs):
        return enqueue.enqueue_changed_sources(str(self.project.root), self.config,
                                               copy.deepcopy(self.changed), engine='claude', **kwargs)

    def rows(self):
        return [json.loads(line) for line in self.queue.read_text().splitlines()] if self.queue.exists() else []

    def test_session_and_post_sync_exclude_changes_and_deletions_before_selection(self):
        paths = ['a' + sep + '/x.md' for sep in ('\x85', '\u2028', '\u2029')]
        for path in paths:
            self.project.write_note('docs/' + path, 'raw bytes\n')
        entry = {'root': str(self.project.root / 'docs'), 'created': paths[:1], 'updated': paths[1:],
                 'deleted': ['deleted' + path for path in paths], 'revisions': {}}
        entry['revisions'] = {path: 'a' * 64 for path in [*paths, *entry['deleted']]}
        self.changed = {'docs': entry}
        selected = ({'recallSettings': {'skipPaths': []}, 'installId': 'a' * 32,
                     'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32}, {})
        with patch.object(workspace, 'selected', return_value=selected), patch.object(workspace, 'request_compile') as wake:
            for session in (True, False):
                self.assertEqual(self.run_enqueue(session_start=session)['queued'], 0)
            wake.assert_not_called()
            entry['updated'].append('b.md')
            self.assertEqual(self.run_enqueue(session_start=True)['queued'], 1)
            self.assertEqual([r['source']['path'] for r in self.rows()], ['docs/b.md'])

    def test_sync_queues_eligible_sources_in_cap_order(self):
        result = self.run_enqueue()
        self.assertEqual((result['queued'], result['deferred'], result['reason']), (4, 0, 'queued'))
        rows = self.rows()
        self.assertEqual([(row['source']['collection'], row['source']['path']) for row in rows],
                         [('docs', 'docs/a.md'), ('docs', 'docs/b.md'), ('docs', 'docs/c.md'), ('src', 'src/s.md')])
        self.assertEqual({row['trigger'] for row in rows}, {'post_sync_source'})
        self.assertEqual({row['engine'] for row in rows}, {'claude'})
        self.assertTrue(all('contentRevision' in row and 'content' not in row['source'] for row in rows))

    def test_session_start_records_its_own_trigger(self):
        self.assertEqual(self.run_enqueue(session_start=True)['reason'], 'queued')
        self.assertEqual({row['trigger'] for row in self.rows()}, {'session_start_source'})

    def test_sync_keeps_caller_input_and_sources_while_recording_revisions(self):
        import hashlib
        changed = copy.deepcopy(self.changed)
        before = copy.deepcopy(changed)
        source_paths = [self.project.root / path for path in
                        ('docs/a.md', 'docs/b.md', 'docs/c.md', 'src/s.md')]
        original = {str(path): path.read_bytes() for path in source_paths}
        result = enqueue.enqueue_changed_sources(str(self.project.root), self.config, changed, engine='claude')
        self.assertEqual(result['queued'], 4)
        self.assertEqual(changed, before)
        for row in self.rows():
            path = self.project.root / row['source']['path']
            self.assertEqual(row['contentRevision'], hashlib.sha256(original[str(path)]).hexdigest())
            self.assertEqual(path.read_bytes(), original[str(path)])
        queued = self.queue.read_bytes()
        self.assertEqual(enqueue.enqueue_changed_sources(str(self.project.root), self.config,
                                                         changed, engine='claude')['queued'], 0)
        self.assertEqual(self.queue.read_bytes(), queued)

    def test_manual_sync_queue_record_preserves_fields_and_revision(self):
        import hashlib
        path = self.project.root / 'docs/a.md'
        self.changed = {'docs': {'root': str(path.parent), 'created': ['a.md'],
                                 'updated': [], 'deleted': []}}
        timestamp = '2026-10-17T00:00:00Z'
        with patch.object(enqueue, '_utc_now', return_value=timestamp):
            self.assertEqual(self.run_enqueue()['queued'], 1)
        self.assertEqual(self.rows(), [{
            'ts': timestamp, 'trigger': 'post_sync_source', 'engine': 'claude',
            'cwd': str(self.project.root),
            'source': {'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs'},
            'contentRevision': hashlib.sha256(path.read_bytes()).hexdigest(),
        }])

    def test_session_rows_with_known_revisions_keep_the_other_fields(self):
        import hashlib
        with patch.object(enqueue, '_utc_now', return_value='2026-10-17T00:00:00Z'):
            self.assertEqual(self.run_enqueue(session_start=True)['queued'], 4)
            without_revision = self.rows()
            self.queue.unlink()
            expected = {}
            for entry in self.changed.values():
                entry['revisions'] = {}
                for rel in entry['created'] + entry['updated']:
                    path = self.project.root / entry['root'] / rel
                    if path.is_file():
                        digest = hashlib.sha256(path.read_bytes()).hexdigest()
                        entry['revisions'][rel] = digest
                        expected[path.relative_to(self.project.root).as_posix()] = digest
            self.assertEqual(self.run_enqueue(session_start=True)['queued'], 4)
        revised = self.rows()
        for row in revised:
            self.assertEqual(row.pop('contentRevision'), expected[row['source']['path']])
        self.assertEqual(revised, without_revision)

    def test_cap_defers_the_tail_and_cap_zero_writes_nothing(self):
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '0'}):
            result = self.run_enqueue()
        self.assertEqual((result['queued'], result['deferred'], result['reason']), (0, 4, 'capped'))
        self.assertFalse(self.queue.exists())
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '2'}):
            result = self.run_enqueue()
        self.assertEqual(result['deferredFiles'], [('docs', 'c.md'), ('src', 's.md')])
        self.assertEqual(len(self.rows()), 2)

    def test_trigger_consent_and_non_sources_fail_closed(self):
        self.config['compile']['triggers'] = ['post_tool_source']
        self.assertEqual(self.run_enqueue(session_start=True)['reason'], 'compile_disabled')
        self.assertFalse(self.queue.exists())
        self.changed = {'docs': {'root': str(self.project.root / 'docs'),
                        'created': ['note.py'], 'updated': ['.draft/hidden.md'], 'deleted': ['a.md']}}
        self.assertEqual(self.run_enqueue()['reason'], 'no_sources')

    def test_changed_revision_replaces_only_its_live_slot(self):
        self.assertEqual(self.run_enqueue()['queued'], 4)
        again = self.run_enqueue()
        self.assertEqual(again['queued'], 0)
        self.assertTrue(again.get('wakePending'))
        self.project.write_note('docs/a.md', 'changed content\n')
        changed = self.run_enqueue()
        self.assertEqual(changed['queued'], 1)
        rows = self.rows()
        self.assertEqual([r['source']['path'] for r in rows], ['docs/a.md', 'docs/b.md', 'docs/c.md', 'src/s.md'])
        self.assertEqual(rows[0]['contentRevision'], enqueue._stable_revision(self.project.root / 'docs/a.md'))

    def test_symlinked_compile_dir_is_rejected_before_planning(self):
        outside = self.home / 'outside'
        outside.mkdir(mode=0o700)
        (self.project.root / '.memvane' / 'compile').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_enqueue()['reason'], 'queue_path_rejected')
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_rows_are_preserved_and_new_rows_append(self):
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.queue.write_text('not json\n{"prior": true}\n')
        self.assertEqual(self.run_enqueue()['queued'], 4)
        lines = self.queue.read_text().splitlines()
        self.assertEqual(lines[0], 'not json')
        rows = [json.loads(line) for line in lines[1:]]
        self.assertEqual(rows[0], {'prior': True})
        self.assertEqual(len(rows), 5)
        before = self.queue.read_bytes()
        self.assertEqual(self.run_enqueue()['queued'], 0)
        self.assertEqual(self.queue.read_bytes(), before)

    def test_claimed_rows_are_not_suppressed_by_old_queue_keys(self):
        self.assertEqual(self.run_enqueue()['queued'], 4)
        claimed = self.queue.with_name(self.queue.name + '.claimed.synthetic')
        self.queue.rename(claimed)
        before = claimed.read_bytes()
        self.assertEqual(self.run_enqueue()['queued'], 4)
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(claimed.read_bytes(), before)

    def test_invalid_cap_falls_back_to_default(self):
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': 'many'}):
            result = self.run_enqueue()
        self.assertEqual((result['queued'], result['reason']), (4, 'queued'))

    def test_skipped_rows_use_cap_slots_but_are_not_deferred(self):
        only_a = {'docs': {'root': str(self.project.root / 'docs'),
                           'created': [], 'updated': ['a.md'], 'deleted': []}}
        self.changed = only_a
        self.assertEqual(self.run_enqueue()['queued'], 1)
        self.changed = {'docs': {'root': str(self.project.root / 'docs'),
                                 'created': ['a.md', 'b.md', 'c.md'], 'updated': [], 'deleted': []},
                        'src': {'root': str(self.project.root / 'src'),
                                'created': ['s.md'], 'updated': [], 'deleted': []}}
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '2'}):
            result = self.run_enqueue()
        self.assertEqual((result['queued'], result['deferred'], result.get('wakePending')),
                         (1, 2, True))
        self.assertEqual(result['deferredFiles'], [('docs', 'c.md'), ('src', 's.md')])
        self.assertEqual([r['source']['path'] for r in self.rows()], ['docs/a.md', 'docs/b.md'])

    def test_oversized_queue_defers_without_reading_or_acknowledging(self):
        self.assertEqual(enqueue.QUEUE_SCAN_MAX_BYTES, 8 * 1024 * 1024)
        self.assertEqual(self.run_enqueue()['queued'], 4)
        size = self.queue.stat().st_size
        self.assertEqual(len(enqueue._queued_revision_keys(self.queue, limit=size)), 4)
        self.assertEqual(enqueue._queued_revision_keys(self.queue, limit=size - 1), set())
        with self.queue.open('r+b') as handle:
            handle.truncate(enqueue.QUEUE_SCAN_MAX_BYTES + 1)
        before = self.queue.stat()
        with patch.object(enqueue.source_queue, '_open_read', side_effect=AssertionError('read')):
            with self.assertRaisesRegex(OSError, 'read_limit'):
                self.run_enqueue()
        after = self.queue.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_incomplete_tail_and_row_limits_leave_raw_bytes_at_their_original_name(self):
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        for raw, reason in ((b'{}\n{"cut":', 'incomplete'), (b'{"valid":true}', 'incomplete'),
                            (b'x' * 65536 + b'\n', 'oversized'), (b'{}\n' * 4097, 'row_limit'),
                            (b'{}\n' * 4096, 'row_limit')):
            with self.subTest(reason=reason):
                self.queue.write_bytes(raw)
                inode = self.queue.stat().st_ino
                with self.assertRaisesRegex(enqueue.source_queue.HandoffRequired, reason):
                    self.run_enqueue()
                self.assertEqual((self.queue.stat().st_ino, self.queue.read_bytes()), (inode, raw))

    def test_only_lf_separates_raw_rows_and_ambiguous_rows_stay_opaque(self):
        self.run_enqueue()
        first, *_ = self.rows()
        encoded = json.dumps(first).encode()
        duplicates = b'{"trigger":"extra",' + encoded[1:]
        raw = b'broken\r' + encoded + b'\n' + duplicates + b'\n'
        other = dict(first, selectedRoute={'installId': 'other', 'scopeId': 'scope'})
        raw += json.dumps(other, indent=None, separators=(', ', ' : ')).encode() + b'\r\n'
        self.queue.write_bytes(raw)
        self.assertEqual(self.run_enqueue()['queued'], 4)
        self.assertTrue(self.queue.read_bytes().startswith(raw))
        self.assertEqual(len(self.queue.read_bytes().split(b'\n')) - 1, 7)

    def test_contended_claim_lock_refuses_publication_without_blocking(self):
        self.run_enqueue()
        before = self.queue.read_bytes()
        self.project.write_note('docs/a.md', 'other bytes')
        lock_path = workspace.queue_lock_path(self.queue)
        with lock_path.open('a') as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            flock = fcntl.flock
            def nonblocking(fd, flags):
                self.assertEqual(flags, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return flock(fd, flags)
            with patch.object(fcntl, 'flock', side_effect=nonblocking):
                with self.assertRaises(BlockingIOError):
                    self.run_enqueue()
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(self.run_enqueue()['queued'], 1)

    def test_concurrent_claim_and_new_producer_do_not_get_overwritten(self):
        self.run_enqueue()
        prior = self.queue.read_bytes()
        self.project.write_note('docs/a.md', 'next')
        other = b'{"other":"producer"}\n'
        claimed = self.queue.with_name(self.queue.name + '.claimed.9999.fixture')
        publish = enqueue.source_queue.publish
        def race(path, original, image, verified):
            path.rename(claimed)
            path.write_bytes(other)
            return publish(path, original, image, verified)
        with patch.object(enqueue.source_queue, 'publish', side_effect=race):
            with self.assertRaisesRegex(BlockingIOError, 'changed'):
                self.run_enqueue()
        self.assertEqual(claimed.read_bytes(), prior)
        self.assertEqual(self.queue.read_bytes(), other)
        self.assertEqual(self.run_enqueue()['queued'], 4)
        self.assertTrue(self.queue.read_bytes().startswith(other))

    def test_process_exit_before_and_after_each_image_boundary_replays_a_whole_batch(self):
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        old = b'{"source":{"collection":"docs","path":"docs/a.md"},"contentRevision":"old"}\n'
        raw = b'opaque\xff\r\n'
        args = [sys.executable, '-B', '-c',
                'from memvane.wiki.intake.tests.integration.test_source_enqueue import _crash_producer; '
                'import sys; _crash_producer(*sys.argv[1:])', str(self.queue)]
        for boundary in ('reserve', 'file_sync', 'verify', 'rename', 'dir_sync'):
            for side in ('before', 'after'):
                with self.subTest(boundary=boundary, side=side):
                    self.queue.write_bytes(raw + old)
                    result = subprocess.run(args + [boundary, side], capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 91, result.stderr)
                    published = boundary == 'dir_sync' or (boundary == 'rename' and side == 'after')
                    after_crash = self.queue.read_bytes()
                    self.assertTrue(after_crash.startswith(raw))
                    if published:
                        rows = [json.loads(line) for line in after_crash[len(raw):].split(b'\n') if line]
                        self.assertEqual([r['contentRevision'] for r in rows], ['new', 'new'])
                    else:
                        self.assertEqual(after_crash, raw + old)
                    subprocess.run(args + ['replay', ''], check=True, timeout=20)
                    stable = self.queue.read_bytes()
                    subprocess.run(args + ['replay', ''], check=True, timeout=20)
                    self.assertEqual(self.queue.read_bytes(), stable)
                    rows = [json.loads(line) for line in stable[len(raw):].split(b'\n') if line]
                    self.assertEqual([(r['source']['path'], r['contentRevision']) for r in rows],
                                     [('docs/a.md', 'new'), ('docs/b.md', 'new')])

    def test_corrupted_prepared_image_is_rejected_before_rename(self):
        self.run_enqueue()
        before = self.queue.read_bytes()
        self.project.write_note('docs/a.md', 'next')
        verify = enqueue.source_queue._verify_image
        def corrupt(image, size, digest):
            image.seek(0)
            image.write(b'!')
            image.flush()
            return verify(image, size, digest)
        with patch.object(enqueue.source_queue, '_verify_image', side_effect=corrupt):
            with self.assertRaisesRegex(OSError, 'image_mismatch'):
                self.run_enqueue()
        self.assertEqual(self.queue.read_bytes(), before)

    def test_dirsync_failure_retries_without_duplicating_any_published_row(self):
        with patch.object(enqueue.source_queue, 'fsync_dir', side_effect=OSError('directory_failure')):
            with self.assertRaisesRegex(OSError, 'directory_failure'):
                self.run_enqueue()
            # A retry that recognizes all rows has the same durability obligation.
            with self.assertRaisesRegex(OSError, 'directory_failure'):
                self.run_enqueue()
        before = self.queue.read_bytes()
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.run_enqueue()['queued'], 0)
        self.assertEqual(self.queue.read_bytes(), before)

    def test_same_content_is_queued_once_per_trigger(self):
        import hashlib
        for entry in self.changed.values():
            entry['revisions'] = {rel: hashlib.sha256((self.project.root / entry['root'] / rel).read_bytes()).hexdigest()
                                  for rel in entry['created'] + entry['updated']
                                  if (self.project.root / entry['root'] / rel).is_file()}
        self.assertEqual(self.run_enqueue(session_start=True)['queued'], 4)
        self.assertEqual(self.run_enqueue(session_start=False)['queued'], 4)
        self.assertEqual(self.run_enqueue(session_start=False)['queued'], 0)
        rows = self.rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual({row['trigger'] for row in rows},
                         {'session_start_source', 'post_sync_source'})
        self.assertTrue(all(row.get('contentRevision') for row in rows))

    def test_edit_collapses_legacy_duplicates_preserving_flags_routes_and_unreadable_lines(self):
        self.changed = {'docs': {'root': str(self.project.root / 'docs'),
                                 'created': [], 'updated': ['a.md'], 'deleted': []}}
        self.run_enqueue()
        current, = self.rows()
        old = dict(current, contentRevision='old')
        separate = [dict(old, **flags) for flags in (
            {'sourceDelete': True}, {'forceRecompile': True},
            {'sourceDelete': True, 'sharedEnqueued': True}, {'initialBackfill': 'a' * 32},
            {'trigger': 'session_start_source'}, {'selectedRoute': {'installId': 'other', 'scopeId': 'scope'}})]
        opaque = b'not JSON: \xff\n'
        payload = b''.join((json.dumps(row) + '\n').encode() for row in [old, current, *separate])
        self.queue.write_bytes(opaque + payload)
        self.assertEqual(self.run_enqueue()['queued'], 1)
        raw = self.queue.read_bytes()
        self.assertTrue(raw.startswith(opaque))
        rows = [json.loads(line) for line in raw[len(opaque):].splitlines()]
        self.assertEqual(rows[0]['contentRevision'], current['contentRevision'])
        self.assertEqual(rows[1:], separate)

    def test_sync_aliases_share_one_slot_without_acknowledging_a_deferred_file(self):
        docs = self.project.root / 'docs'
        (docs / 'alias.md').symlink_to(docs / 'a.md')
        self.changed = {'docs': {'root': str(docs), 'created': ['a.md', 'alias.md', 'b.md'],
                                 'updated': [], 'deleted': []}}
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '2'}):
            first = self.run_enqueue()
            replay = self.run_enqueue()
        self.assertEqual((first['queued'], first['deferredFiles']), (1, [('docs', 'b.md')]))
        self.assertEqual((replay['queued'], replay['deferredFiles']), (0, [('docs', 'b.md')]))
        self.assertEqual([row['source']['path'] for row in self.rows()], ['docs/a.md'])

    def test_sync_batch_compares_its_last_observation_and_carries_partial_work(self):
        self.run_enqueue()
        current, *neighbors = self.rows()
        older = dict(current, contentRevision='older')
        def put(rows):
            return enqueue._append_unqueued(self.queue, [('docs', 'a.md', row) for row in rows], len(rows))
        self.assertEqual(put([older, current])['result']['queued'], 0)
        self.assertEqual(self.rows(), [current, *neighbors])
        partial = dict(older, partialCompile=True, _processedContentRevision='frozen')
        updated = dict(current, partialCompile=True, _processedContentRevision='frozen')
        self.assertEqual(put([partial, current])['result']['queued'], 1)
        self.assertEqual(self.rows(), [updated, *neighbors])

    def test_producer_and_claimed_worker_keep_independent_operation_occurrences(self):
        row = {'source': {'collection': 'docs', 'path': 'docs/a.md'}, 'contentRevision': 'one'}
        force = dict(row, forceRecompile=True)
        frozen = dict(row, initialBackfill='f' * 32)
        delete = dict(row, sourceDelete=True)
        wait = dict(delete, sharedEnqueued=True)
        expected = [force, force]
        for first in (frozen, delete, wait):
            expected += [first, dict(first, contentRevision='two')]
        # A repeated frozen item is the same cohort/path/revision. Equal force
        # payloads have no parent ID proving the two requests are the same work.
        enqueue._append_jsonl(self.queue, [*expected, frozen])
        self.assertEqual(self.rows(), expected)
        enqueue._append_jsonl(self.queue, [frozen, force])
        expected.append(force)
        self.assertEqual(self.rows(), expected)
        observed = [(json.dumps(item), item) for item in [*self.rows(), frozen]]
        kept, dropped = enqueue.intent.coalesce_claimed_jobs(observed)
        self.assertEqual([item for _, item in kept], expected)
        original_frozen = next(raw for raw, item in observed if item == frozen)
        self.assertEqual(dropped, [original_frozen])
        self.assertEqual(next(raw for raw, item in kept if item == frozen), observed[-1][0])

    def test_one_batch_retains_only_the_last_revision_even_if_already_live(self):
        self.run_enqueue()
        first, *others = self.rows()
        older = dict(first, contentRevision='older')
        enqueue._append_jsonl(self.queue, [older, first])
        self.assertEqual(self.rows(), [first, *others])
        self.queue.unlink()
        enqueue._append_jsonl(self.queue, [older, *others, first])
        self.assertEqual(self.rows(), [first, *others])

    def test_failed_atomic_replacement_keeps_original_and_propagates_to_sync(self):
        self.run_enqueue()
        before = self.queue.read_bytes()
        self.project.write_note('docs/a.md', 'replacement')
        with patch.object(os, 'replace', side_effect=OSError('fixture_write_failure')):
            with self.assertRaisesRegex(OSError, 'fixture_write_failure'):
                self.run_enqueue()
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(list(self.queue.parent.glob('.source-queue-*')), [])
