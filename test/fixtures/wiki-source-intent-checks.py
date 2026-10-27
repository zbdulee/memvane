"""Pure wiki source-intent planning and the enqueue path wired through it.

Synthetic values and a disposable project under the disposable HOME the runner
provides. No QMD, daemon, model, extractor or network: the source queue file is
the only product effect, and it lives inside that project.
"""
import ast
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path.cwd() / 'core'))
import compile_paths as cp
import config as qmd_config
import file_change_plan
import wiki_compile_enqueue as E
import wiki_source_intent as I

FROZEN_TS = '2026-10-17T00:00:00Z'


def legacy_enqueue(project_root, config, changed_files, *, session_start=False):
    """The pre-boundary `enqueue_changed_sources`, kept verbatim as the oracle."""
    accepted = ('session_start_source',) if session_start else ('post_sync_source', 'post_tool_source')
    compile_cfg = E.compile_gate(config, accepted)
    if compile_cfg is None:
        return {"queued": 0, "deferred": 0, "deferredFiles": [], "reason": "compile_disabled"}
    queue_path = E._safe_queue_path(project_root, cp.rel(cp.SOURCE_QUEUE))
    if queue_path is None:
        return {"queued": 0, "deferred": 0, "deferredFiles": [], "reason": "queue_path_rejected"}
    trigger = 'session_start_source' if session_start else 'post_sync_source'
    engine = E.compile_engine(compile_cfg)
    candidates = []
    for name in sorted(changed_files):
        entry = changed_files[name]
        root = Path(entry["root"])
        for rel in sorted(set(entry["created"]) | set(entry["updated"])):
            record = E._source_record(str(root / rel), project_root, project_root,
                                      config, engine, trigger=trigger)
            if record is not None:
                candidates.append((name, rel, record))
    if not candidates:
        return {"queued": 0, "deferred": 0, "deferredFiles": [], "reason": "no_sources"}
    cap = E.compile_max_files()
    picked = candidates[:cap]
    deferred = [(name, rel) for name, rel, _ in candidates[cap:]]
    E._append_jsonl(queue_path, [record for _, _, record in picked])
    return {"queued": len(picked), "deferred": len(deferred), "deferredFiles": deferred,
            "reason": "capped" if deferred else "queued"}


class PurePlanning(unittest.TestCase):
    def test_module_imports_only_the_standard_library_nothing_at_all(self):
        tree = ast.parse(Path('core/wiki_source_intent.py').read_text(encoding='utf-8'))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], 'the planning boundary must stay free of effects')

    def test_trigger_policy_keeps_consent_per_caller(self):
        self.assertEqual(I.trigger_policy(False),
                         (('post_sync_source', 'post_tool_source'), 'post_sync_source'))
        # SessionStart accepts only its own grant; post_tool consent is not widened.
        self.assertEqual(I.trigger_policy(True), (('session_start_source',), 'session_start_source'))
        for label in ('post_sync_source', 'post_tool_source', 'session_start_source'):
            self.assertIn(label, qmd_config.COMPILE_TRIGGERS)

    def test_enqueue_module_aliases_are_the_same_labels(self):
        self.assertEqual(E.POST_TOOL_TRIGGER, 'post_tool_source')
        self.assertEqual(E.COMPILE_TRIGGER, 'post_sync_source')
        self.assertEqual(E.COMPILE_ACCEPTED_TRIGGERS, ('post_sync_source', 'post_tool_source'))

    def test_changed_paths_are_sorted_deduplicated_and_skip_deletes(self):
        changed = {
            'zeta': {'root': '/w/z', 'created': ['b.md'], 'updated': ['a.md', 'b.md'], 'deleted': ['c.md']},
            'alpha': {'root': '/w/a', 'created': [], 'updated': [], 'deleted': ['gone.md']},
            'mid': {'root': '/w/m', 'created': ['y.md', 'x.md'], 'updated': [], 'deleted': []},
        }
        before = copy.deepcopy(changed)
        self.assertEqual(list(I.changed_source_paths(changed)), [
            ('mid', '/w/m', 'x.md'), ('mid', '/w/m', 'y.md'),
            ('zeta', '/w/z', 'a.md'), ('zeta', '/w/z', 'b.md')])
        self.assertEqual(changed, before)

    def test_plan_splits_by_cap_in_candidate_order(self):
        rows = [('c', f'{i}.md', {'n': i}) for i in range(3)]
        cases = {
            50: ([0, 1, 2], [], 'queued'),
            3: ([0, 1, 2], [], 'queued'),
            2: ([0, 1], [('c', '2.md')], 'capped'),
            0: ([], [('c', '0.md'), ('c', '1.md'), ('c', '2.md')], 'capped'),
        }
        for cap, (picked, deferred, reason) in cases.items():
            plan = I.plan_enqueue(iter(rows), cap)
            self.assertEqual([r['n'] for r in plan['records']], picked, cap)
            self.assertEqual(plan['result'], {'queued': len(picked), 'deferred': len(deferred),
                                              'deferredFiles': deferred, 'reason': reason}, cap)
            # Records are passed through, not copied or rewritten.
            for record in plan['records']:
                self.assertIs(record, rows[record['n']][2])

    def test_empty_plan_and_result_shape(self):
        self.assertEqual(I.plan_enqueue([], 5),
                         {'records': [], 'result': {'queued': 0, 'deferred': 0,
                                                    'deferredFiles': [], 'reason': 'no_sources'}})
        first, second = I.enqueue_result('x'), I.enqueue_result('x')
        self.assertIsNot(first['deferredFiles'], second['deferredFiles'])

    def test_intent_identity_is_the_hook_duplicate_key(self):
        record = {'cwd': '/p', 'trigger': 't', 'engine': 'e', 'ts': 'now',
                  'source': {'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs'}}
        self.assertEqual(I.intent_identity(record), ('/p', 'docs/a.md', 'docs'))

    def test_revision_key_keeps_trigger_and_ignores_the_owner_path(self):
        def row(cwd, trigger, **extra):
            return dict({'cwd': cwd, 'trigger': trigger, 'engine': 'e', 'ts': 'now',
                         'source': {'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs'}},
                        **extra)
        revised = {'contentRevision': 'r1'}
        # The queue is project-owned; a respelled root must not split one intent.
        self.assertEqual(I.revision_key(row('/p', 'session_start_source', **revised)),
                         I.revision_key(row('/private/p', 'session_start_source', **revised)))
        self.assertNotEqual(I.revision_key(row('/p', 'session_start_source', **revised)),
                            I.revision_key(row('/p', 'post_sync_source', **revised)))
        # PostToolUse, manual sync and recovery rows have no revision, hence no key.
        self.assertIsNone(I.revision_key(row('/p', 'post_tool_source')))
        self.assertIsNone(I.revision_key(row('/p', 'session_start_source', contentRevision='')))
        self.assertIsNone(I.revision_key(['not', 'a', 'row']))


class ProjectFixture(unittest.TestCase):
    """A disposable project with raw/source/wiki collections and a fixed clock."""

    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix='qmd-wiki-source-intent-',
                                     dir=os.environ['HOME'])).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        self.project = base / 'project'
        for rel, text in {
            'docs/a.md': '# a\n', 'docs/b.md': '# b\n', 'docs/c.md': '# c\n',
            'docs/note.py': 'print(1)\n', 'docs/.draft/h.md': '# hidden\n',
            'src/s.md': '# source role\n', 'wiki/w.md': '# wiki role\n',
        }.items():
            path = self.project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        self.config = {
            'indexing': True,
            'collections': ['docs', 'src', 'wiki'],
            'collectionPaths': {'docs': 'docs', 'src': 'src', 'wiki': 'wiki'},
            'collectionRoles': {'docs': 'raw', 'src': 'source', 'wiki': 'wiki'},
            'compile': {'mode': 'auto-wiki',
                        'triggers': ['post_sync_source', 'session_start_source'],
                        'extractor': {'builtins': ['claude']}},
        }
        root = lambda name: str(self.project / name)
        self.changed = {
            'docs': {'root': root('docs'), 'created': ['c.md', 'note.py', '.draft/h.md'],
                     'updated': ['b.md', 'a.md'], 'deleted': ['gone.md']},
            'src': {'root': root('src'), 'created': ['s.md'], 'updated': [], 'deleted': []},
            'wiki': {'root': root('wiki'), 'created': ['w.md'], 'updated': [], 'deleted': []},
        }
        patches = [mock.patch.object(E, '_utc_now', return_value=FROZEN_TS),
                   mock.patch.dict(os.environ, {'QMD_ENGINE': 'claude'})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.queue = self.project / cp.rel(cp.SOURCE_QUEUE)

    def source_digest(self):
        files = sorted(p for p in self.project.rglob('*')
                       if p.is_file() and '.auto-context' not in p.parts)
        return {str(p.relative_to(self.project)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files}

    def drain(self):
        data = self.queue.read_bytes() if self.queue.exists() else None
        if self.queue.exists():
            self.queue.unlink()
        return data

    def rows(self, data):
        return [json.loads(line) for line in data.decode('utf-8').splitlines()]


class EnqueueThroughSeam(ProjectFixture):
    """Legacy queue fields remain exact; manual sync adds only source revision."""

    def compare(self, config=None, *, session_start=False, cap=None):
        config = config or self.config
        env = {} if cap is None else {'QMD_SYNC_COMPILE_MAX': str(cap)}
        sources = self.source_digest()
        with mock.patch.dict(os.environ, env):
            legacy = legacy_enqueue(str(self.project), config, copy.deepcopy(self.changed),
                                    session_start=session_start)
            legacy_bytes = self.drain()
            changed = copy.deepcopy(self.changed)
            current = E.enqueue_changed_sources(str(self.project), config, changed,
                                                session_start=session_start)
            current_bytes = self.drain()
        self.assertEqual(current, legacy)
        if current_bytes is None or legacy_bytes is None:
            self.assertEqual(current_bytes, legacy_bytes)
        else:
            current_rows = self.rows(current_bytes)
            legacy_rows = self.rows(legacy_bytes)
            for row in current_rows:
                if not session_start:
                    source = self.project / row['source']['path']
                    self.assertEqual(row.pop('contentRevision'),
                                     hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(current_rows, legacy_rows,
                             'all existing queue fields and order stay unchanged')
        self.assertEqual(changed, self.changed, 'the change set is not mutated')
        self.assertEqual(self.source_digest(), sources, 'source files are never written')
        return current, current_bytes

    def test_sync_queues_eligible_markdown_in_cap_order(self):
        result, data = self.compare()
        self.assertEqual(result, {'queued': 4, 'deferred': 0, 'deferredFiles': [], 'reason': 'queued'})
        rows = self.rows(data)
        self.assertEqual([(r['source']['collection'], r['source']['path']) for r in rows], [
            ('docs', 'docs/a.md'), ('docs', 'docs/b.md'), ('docs', 'docs/c.md'), ('src', 'src/s.md')])
        self.assertEqual({r['trigger'] for r in rows}, {'post_sync_source'})
        self.assertEqual({r['engine'] for r in rows}, {'claude'})
        self.assertEqual({r['ts'] for r in rows}, {FROZEN_TS})

    def test_session_start_records_its_own_trigger(self):
        result, data = self.compare(session_start=True)
        self.assertEqual(result['reason'], 'queued')
        self.assertEqual({r['trigger'] for r in self.rows(data)}, {'session_start_source'})

    def test_cap_defers_the_tail_and_cap_zero_writes_nothing(self):
        result, data = self.compare(cap=2)
        # Deferred files are collection-root relative: the snapshot rollback keys.
        self.assertEqual(result, {'queued': 2, 'deferred': 2, 'reason': 'capped',
                                  'deferredFiles': [('docs', 'c.md'), ('src', 's.md')]})
        self.assertEqual(len(self.rows(data)), 2)
        result, data = self.compare(cap=0)
        self.assertEqual((result['queued'], result['deferred'], result['reason']), (0, 4, 'capped'))
        self.assertIsNone(data, 'an all-deferred wake must not touch the queue file')

    def test_invalid_cap_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': 'many'}):
            result, _ = self.compare()
        self.assertEqual(result['reason'], 'queued')

    def test_consent_gates_and_rejections_match(self):
        post_tool_only = copy.deepcopy(self.config)
        post_tool_only['compile']['triggers'] = ['post_tool_source']
        # Manual sync honours post_tool consent; SessionStart does not.
        self.assertEqual(self.compare(post_tool_only)[0]['reason'], 'queued')
        result, data = self.compare(post_tool_only, session_start=True)
        self.assertEqual((result['reason'], data), ('compile_disabled', None))
        off = copy.deepcopy(self.config)
        off['compile']['mode'] = 'off'
        self.assertEqual(self.compare(off)[0]['reason'], 'compile_disabled')
        not_indexing = dict(self.config, indexing=False)
        self.assertEqual(self.compare(not_indexing)[0]['reason'], 'compile_disabled')

    def test_no_sources_when_only_ineligible_paths_changed(self):
        self.changed = {'docs': {'root': str(self.project / 'docs'), 'created': ['note.py'],
                                 'updated': ['.draft/h.md'], 'deleted': ['a.md']},
                        'wiki': {'root': str(self.project / 'wiki'), 'created': ['w.md'],
                                 'updated': [], 'deleted': []}}
        result, data = self.compare()
        self.assertEqual((result['reason'], data), ('no_sources', None))

    def test_symlinked_compile_dir_is_rejected_before_planning(self):
        outside = self.project.parent / 'outside'
        outside.mkdir()
        (self.project / '.auto-context').mkdir()
        (self.project / '.auto-context' / 'compile').symlink_to(outside, target_is_directory=True)
        result, data = self.compare()
        self.assertEqual((result['reason'], data), ('queue_path_rejected', None))
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_queue_rows_are_appended_after(self):
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        prior = b'{"prior": true}\n'
        self.queue.write_bytes(prior)
        E.enqueue_changed_sources(str(self.project), self.config, copy.deepcopy(self.changed))
        self.assertTrue(self.queue.read_bytes().startswith(prior))
        self.assertEqual(len(self.queue.read_bytes().splitlines()), 5)

    def test_deferred_files_keep_snapshot_entries_and_ack_sees_them(self):
        # The file-change apply contract consumes `deferredFiles` from this seam.
        meta = lambda n: {'mtimeNs': n, 'size': n}
        previous = {'collections': {
            'docs': {'root': str(self.project / 'docs'), 'files': {'a.md': meta(1), 'b.md': meta(1)}},
            'src': {'root': str(self.project / 'src'), 'files': {}}}}
        snapshot = {'collections': {
            'docs': {'root': str(self.project / 'docs'),
                     'files': {'a.md': meta(2), 'b.md': meta(2), 'c.md': meta(1)}},
            'src': {'root': str(self.project / 'src'), 'files': {'s.md': meta(1)}}}}
        persisted, acked = [], []
        with mock.patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '1'}):
            compiled = file_change_plan.apply_change_set(
                {}, copy.deepcopy(self.changed), snapshot, previous,
                enqueue_index=None,
                enqueue_wiki=lambda files: E.enqueue_changed_sources(
                    str(self.project), self.config, files),
                persist=lambda value: persisted.append(copy.deepcopy(value)),
                ack=acked.append)
        self.assertEqual(compiled['deferredFiles'],
                         [('docs', 'b.md'), ('docs', 'c.md'), ('src', 's.md')])
        files = persisted[0]['collections']
        self.assertEqual(files['docs']['files'], {'a.md': meta(2), 'b.md': meta(1)})
        self.assertEqual(files['src']['files'], {})
        # A deferred wake is reported to ACK, so the pending event is kept.
        self.assertEqual(acked, [compiled])
        self.assertEqual(compiled['deferred'], 3)
        self.assertEqual(len(self.rows(self.queue.read_bytes())), 1)


class RevisionCoalescing(ProjectFixture):
    """A hashed row identical to one still in the live queue is not appended."""

    NO_SOURCES = {'queued': 0, 'deferred': 0, 'deferredFiles': [], 'reason': 'no_sources'}
    # Skipped rows are still live: the internal wake signal, never a count.
    SKIPPED_ALL = {**NO_SOURCES, 'wakePending': True}

    def hashed(self, revision='r1', changed=None):
        changed = copy.deepcopy(changed or self.changed)
        for entry in changed.values():
            entry['revisions'] = {rel: f'{revision}:{rel}'
                                  for rel in entry['created'] + entry['updated']}
        return changed

    def enqueue(self, changed, *, session_start=True, cap=None):
        env = {} if cap is None else {'QMD_SYNC_COMPILE_MAX': str(cap)}
        with mock.patch.dict(os.environ, env):
            return E.enqueue_changed_sources(str(self.project), self.config, changed,
                                             session_start=session_start)

    def test_same_key_still_queued_is_skipped_as_no_sources(self):
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        foreign = b'not json\n{"prior": true}\n'
        self.queue.write_bytes(foreign)
        first = self.enqueue(self.hashed())
        self.assertEqual(first['queued'], 4)
        self.assertNotIn('wakePending', first, 'nothing skipped, nothing extra to wake')
        before = self.queue.read_bytes()
        self.assertEqual(self.enqueue(self.hashed()), self.SKIPPED_ALL)
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertTrue(before.startswith(foreign), 'unrelated queue lines are left alone')

    def test_new_revision_or_trigger_appends_but_manual_live_retry_dedups(self):
        self.enqueue(self.hashed('r1'))
        self.assertEqual(self.enqueue(self.hashed('r2'))['queued'], 4)
        # The file-change event path hashes too but records post_sync_source.
        self.assertEqual(self.enqueue(self.hashed('r2'), session_start=False)['queued'], 4)
        # Manual sync hashes the source. A still-live same-revision row is not
        # appended a second time, but it retains an internal wake hint.
        self.assertEqual(self.enqueue(copy.deepcopy(self.changed), session_start=False)['queued'], 4)
        self.assertEqual(self.enqueue(copy.deepcopy(self.changed), session_start=False),
                         self.SKIPPED_ALL)
        rows = self.rows(self.queue.read_bytes())
        self.assertEqual(len(rows), 16)
        self.assertEqual(sum('contentRevision' not in r for r in rows), 0)

    def test_skipped_rows_use_cap_slots_but_are_not_deferred(self):
        only_a = {'docs': {'root': str(self.project / 'docs'), 'created': [],
                           'updated': ['a.md'], 'deleted': []}}
        self.enqueue(self.hashed(changed=only_a))
        # a.md is still live and takes one of the two slots, so only b.md is appended.
        self.assertEqual(self.enqueue(self.hashed(), cap=2), {
            'queued': 1, 'deferred': 2, 'deferredFiles': [('docs', 'c.md'), ('src', 's.md')],
            'reason': 'capped', 'wakePending': True})
        before = self.queue.read_bytes()
        self.assertEqual([r['source']['path'] for r in self.rows(before)],
                         ['docs/a.md', 'docs/b.md'])
        # Skipped rows fill the cap: fresh files are held back, skipped ones never are.
        self.assertEqual(self.enqueue(self.hashed(), cap=2), {
            'queued': 0, 'deferred': 2, 'deferredFiles': [('docs', 'c.md'), ('src', 's.md')],
            'reason': 'capped', 'wakePending': True})
        self.assertEqual(self.queue.read_bytes(), before)

    def test_retry_after_snapshot_failure_keeps_the_per_wake_cap(self):
        docs = self.project / 'docs'
        total, cap = 60, 50
        for i in range(total):
            (docs / f'n{i:02d}.md').write_text(f'# n{i}\n', encoding='utf-8')
        scanned = {f'n{i:02d}.md': {'mtimeNs': 1, 'size': 1, 'sha256': hashlib.sha256(
                       (docs / f'n{i:02d}.md').read_bytes()).hexdigest()}
                   for i in range(total)}
        names = sorted(scanned)

        def run(previous, persist, acked):
            plan = file_change_plan.plan_change_set([('docs', str(docs), previous, scanned, ())])
            with mock.patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': str(cap)}):
                return file_change_plan.apply_change_set(
                    {}, plan['changedFiles'], {'collections': plan['collections']},
                    {'collections': {'docs': {'root': str(docs), 'files': previous}}},
                    enqueue_index=None,
                    enqueue_wiki=lambda files: E.enqueue_changed_sources(
                        str(self.project), self.config, files, session_start=True),
                    persist=persist, ack=acked.append)

        def failing(_snapshot):
            raise OSError('synthetic snapshot failure')
        acked = []
        with self.assertRaises(OSError):
            run({}, failing, acked)
        first = self.queue.read_bytes()
        self.assertEqual([r['source']['path'] for r in self.rows(first)],
                         [f'docs/{n}' for n in names[:cap]])
        # The retry sees all 60 again; the 50 live rows fill the cap, the tail stays deferred.
        persisted = []
        compiled = run({}, persisted.append, acked)
        self.assertEqual(self.queue.read_bytes(), first, 'one wake must not compile more than cap')
        self.assertEqual(len({r['source']['path'] for r in self.rows(first)}), cap)
        self.assertEqual(compiled, {'queued': 0, 'deferred': total - cap, 'reason': 'capped',
                                    'deferredFiles': [('docs', n) for n in names[cap:]],
                                    'wakePending': True})
        files = persisted[0]['collections']['docs']['files']
        self.assertEqual(sorted(files), names[:cap], 'the deferred tail keeps no snapshot entry')
        self.assertEqual(acked, [compiled])
        # The wake drains the live queue; the next run picks up exactly the tail.
        self.drain()
        persisted.clear()
        later = run(files, persisted.append, acked)
        self.assertEqual(later, {'queued': total - cap, 'deferred': 0, 'deferredFiles': [],
                                 'reason': 'queued'})
        self.assertEqual([r['source']['path'] for r in self.rows(self.queue.read_bytes())],
                         [f'docs/{n}' for n in names[cap:]])
        self.assertEqual(persisted[0]['collections']['docs']['files'], scanned)

    def test_oversized_live_queue_falls_back_to_plain_append(self):
        self.assertEqual(E.QUEUE_SCAN_MAX_BYTES, 8 * 1024 * 1024)
        self.enqueue(self.hashed())
        live = self.queue.read_bytes()
        keys = E._queued_revision_keys(self.queue, limit=len(live))
        self.assertEqual(len(keys), 4)
        # One byte over the bound: no keys, nothing parsed.
        self.assertEqual(E._queued_revision_keys(self.queue, limit=len(live) - 1), set())
        pad = json.dumps({'pad': 'x' * E.QUEUE_SCAN_MAX_BYTES}).encode('utf-8') + b'\n'
        with open(self.queue, 'ab') as handle:
            handle.write(pad)
        oversized = self.queue.read_bytes()
        self.assertGreater(len(oversized), E.QUEUE_SCAN_MAX_BYTES)
        # Same keys are still live, but the scan is skipped: legacy append, no wake signal.
        self.assertEqual(self.enqueue(self.hashed()),
                         {'queued': 4, 'deferred': 0, 'deferredFiles': [], 'reason': 'queued'})
        data = self.queue.read_bytes()
        self.assertTrue(data.startswith(oversized), 'existing rows are never rewritten or lost')
        self.assertEqual(data[len(oversized):], live, 'the appended rows match the first run')

    def test_retry_after_enqueue_then_snapshot_failure_queues_once_and_acks(self):
        docs = self.project / 'docs'
        scanned = {rel: {'mtimeNs': 1, 'size': 1,
                         'sha256': hashlib.sha256((docs / rel).read_bytes()).hexdigest()}
                   for rel in ('a.md', 'b.md')}

        def run(persist, acked):
            plan = file_change_plan.plan_change_set([('docs', str(docs), {}, scanned, ())])
            return file_change_plan.apply_change_set(
                {}, plan['changedFiles'], {'collections': plan['collections']},
                {'collections': {}}, enqueue_index=None,
                enqueue_wiki=lambda files: E.enqueue_changed_sources(
                    str(self.project), self.config, files, session_start=True),
                persist=persist, ack=acked.append)

        def failing(_snapshot):
            raise OSError('synthetic snapshot failure')
        acked = []
        with self.assertRaises(OSError):
            run(failing, acked)
        self.assertEqual(acked, [])
        first = self.queue.read_bytes()
        self.assertEqual([r['contentRevision'] for r in self.rows(first)],
                         [scanned['a.md']['sha256'], scanned['b.md']['sha256']])
        persisted = []
        compiled = run(persisted.append, acked)
        self.assertEqual(self.queue.read_bytes(), first, 'the retry must not queue a second paid job')
        # Not counted as queued or deferred, but the still-queued rows must be woken.
        self.assertEqual(compiled, self.SKIPPED_ALL)
        self.assertTrue(compiled['queued'] or compiled.get('wakePending'),
                        'the retry must still request the compile kick')
        # Nothing deferred, so the snapshot advances and the caller may ACK.
        self.assertEqual(persisted[0]['collections']['docs']['files'], scanned)
        self.assertEqual(acked, [compiled])

    def test_rows_the_worker_already_claimed_are_not_coalesced(self):
        self.enqueue(self.hashed())
        claimed = self.queue.with_name(self.queue.name + '.claimed.synthetic')
        os.replace(self.queue, claimed)
        before = claimed.read_bytes()
        # Only rows still in the live queue are visible; this is not processed dedup.
        self.assertEqual(self.enqueue(self.hashed())['queued'], 4)
        self.assertEqual(claimed.read_bytes(), before)

    def test_hashed_rows_differ_from_legacy_only_by_revision(self):
        legacy_enqueue(str(self.project), self.config, copy.deepcopy(self.changed),
                       session_start=True)
        legacy = self.rows(self.drain())
        self.enqueue(self.hashed())
        current = self.rows(self.drain())
        self.assertEqual([r.pop('contentRevision') for r in current],
                         [f"r1:{r['source']['path'].split('/', 1)[1]}" for r in legacy])
        self.assertEqual(current, legacy)

    def test_manual_rows_keep_legacy_fields_and_a_live_retry_dedups(self):
        legacy_enqueue(str(self.project), self.config, copy.deepcopy(self.changed))
        legacy = self.rows(self.drain())
        E.enqueue_changed_sources(str(self.project), self.config, copy.deepcopy(self.changed))
        before = self.queue.read_bytes()
        current = self.rows(before)
        for row in current:
            source = self.project / row['source']['path']
            self.assertEqual(row.pop('contentRevision'),
                             hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(current, legacy)
        self.assertEqual(E.enqueue_changed_sources(
            str(self.project), self.config, copy.deepcopy(self.changed)),
            self.SKIPPED_ALL)
        self.assertEqual(self.queue.read_bytes(), before)


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(case)
                               for case in (PurePlanning, EnqueueThroughSeam, RevisionCoalescing))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
