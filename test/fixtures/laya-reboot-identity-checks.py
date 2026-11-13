"""Real temporary install metadata; only OS device numbers change at reboot."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
spec = importlib.util.spec_from_file_location('reboot_seed', HERE / 'laya-journal-reader-checks.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
import laya_install_journal as J
import laya_host_response as response


class PersistentIdentityChecks(unittest.TestCase):
    def test_only_device_is_ignored_and_inputs_are_unchanged(self):
        from persistent_identity import (persistent_identity_equal as equal,
                                         persistent_node_equal as node,
                                         persistent_provider_equal as provider)
        old = {'path': '/fixture', 'dev': 4, 'ino': 8, 'size': 3,
               'mtime': 5, 'ctime': 6, 'mode': 7, 'nlink': 1, 'sha': 'abc'}
        live = {**old, 'dev': 9}
        self.assertTrue(equal(old, live))
        for key in old.keys() - {'dev'}:
            with self.subTest(field=key):
                self.assertFalse(equal(old, {**live, key: None}))
        self.assertFalse(equal(old, {k: v for k, v in live.items() if k != 'dev'}))
        self.assertEqual(old['dev'], 4)
        self.assertEqual(live['dev'], 9)
        self.assertTrue(node([4, 8], [9, 8]))
        self.assertFalse(node([4, 8], [9, 7]))
        self.assertFalse(node([4, 8], [9, 8, 3]))
        rows = [['/fixture', 4, 8, 3, 5, 6]]
        self.assertTrue(provider(rows, [['/fixture', 9, 8, 3, 5, 6]]))
        for index in (0, 2, 3, 4, 5):
            changed = list(rows[0]); changed[index] = None
            self.assertFalse(provider(rows, [changed]))
        self.assertFalse(provider(rows, []))


class RebootStat:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    @property
    def st_dev(self):
        return self.original.st_dev + 1

    def __getitem__(self, key):
        values = list(self.original)
        values[2] += 1
        return values[key]

    def __iter__(self):
        return iter(self[:])


class RebootEntry:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __fspath__(self):
        return self.original.path

    def stat(self, *args, **kwargs):
        return RebootStat(self.original.stat(*args, **kwargs))


class RebootScandir:
    def __init__(self, original):
        self.original = original

    def __iter__(self):
        return self

    def __next__(self):
        return RebootEntry(next(self.original))

    def __enter__(self):
        self.original.__enter__()
        return self

    def __exit__(self, *args):
        return self.original.__exit__(*args)

    def close(self):
        self.original.close()


@contextmanager
def simulated_reboot():
    """Keep fd/path observations consistent and preserve nanosecond metadata."""
    def wrap(original):
        def stat(*args, **kwargs):
            result = original(*args, **kwargs)
            return result if isinstance(result, RebootStat) else RebootStat(result)
        return stat
    with ExitStack() as stack:
        for target, names in ((os, ('stat', 'lstat', 'fstat')),
                              (Path, ('stat', 'lstat'))):
            for name in names:
                stack.enter_context(mock.patch.object(target, name, wrap(getattr(target, name))))
        # CandidateQuery checks syscall support by function identity.
        for name in ('supports_dir_fd', 'supports_follow_symlinks', 'supports_fd'):
            stack.enter_context(mock.patch.object(os, name, getattr(os, name) | {os.stat}))
        scandir = os.scandir
        stack.enter_context(mock.patch.object(os, 'scandir',
            side_effect=lambda *a, **kw: RebootScandir(scandir(*a, **kw))))
        yield


def fixture(filename, name):
    spec = importlib.util.spec_from_file_location('reboot_' + name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


class RebootChecks(unittest.TestCase):
    def setUp(self):
        self.helper = seed.JournalReaderChecks('runTest')
        self.addCleanup(self.helper.doCleanups)

    def case_from(self, filename, name):
        case = fixture(filename, name)('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def test_selected_hooks_notification_and_bundle_cas(self):
        case = self.helper.published_case()
        decision = case.decision()
        payload = {'cwd': str(case.project), 'tool_name': 'Edit',
                   'hook_event_name': 'PostToolUse',
                   'tool_input': {'file_path': str(case.project / 'notes.md')},
                   'tool_response': {'success': True}}
        with self.helper.full_authority(case), simulated_reboot(), \
             mock.patch.object(response, '__file__', str(case.plugin / 'core/laya_host_response.py')), \
             mock.patch('runtime_work.request', return_value={'status': 'queued'}), \
             mock.patch.object(response, '_start_owner'), \
             mock.patch('wiki_compile_enqueue.enqueue_payload', return_value={'queued': 0}):
            # A full selected proof must work before any read-only fallback.
            J.verify_selected_route(decision)
            for action in ('recall', 'gate', 'posttool', 'index', 'compile',
                           'update-queued', 'file-change-event'):
                event = dict(payload)
                if action == 'recall':
                    event = {'cwd': str(case.project), 'prompt': 'otter ledger'}
                elif action == 'gate':
                    event['hook_event_name'] = 'PreToolUse'
                elif action == 'update-queued':
                    event['hook_event_name'] = 'SessionStart'
                elif action == 'file-change-event':
                    event.update(tool_name='Bash', tool_input={'command': 'git switch main'},
                                 tool_response={'exit_code': 0})
                with self.subTest(action=action):
                    output = response.serve_selected(action, 'claude', decision, event)
                    # This fixture's synthetic sources serve an empty context
                    # with or without a reboot; the reboot claim is only that
                    # the selected route is served, not refused.
                    self.assertNotIn('request refused', str(output))
                    if action == 'recall':
                        self.assertEqual(output['hookSpecificOutput']['hookEventName'],
                                         'UserPromptSubmit')
            J.verify_notification_target(decision)
            # Bundle takeover after a reboot is proven with a real bundle in
            # test_real_bundle_provider_and_newer_bundle_takeover.

    def test_real_bundle_provider_and_newer_bundle_takeover(self):
        import laya_product_bundle as bundle
        import laya_active_upgrade as upgrade
        case = self.case_from('laya-product-bundle-checks.py', 'ProductBundleChecks')
        root, first, raw = case.selected_on_host_root()
        attestation, _ = J._attestation(case.journal_path)
        rows = J._provider_file_identity(attestation)
        ownership = (root / '.laya-product/ownership-manifest.json').read_bytes()
        with simulated_reboot():
            self.assertEqual(bundle.inspect_full_plugin_candidate(root), first)
            self.assertTrue(J._provider_identity_equal(rows, J._provider_file_identity(attestation)))
            self.assertEqual((root / '.laya-product/ownership-manifest.json').read_bytes(), ownership)
            upgrade._verify_recovery_project(case.journal_path, json.loads(raw))
        commit = case.commit_v2()
        case.reextract(root)
        # Existing fixture supplies synthetic provider review and bridge authority;
        # upgrade_install, manifest proof, journal CAS and recovery remain real.
        with simulated_reboot(), mock.patch.object(upgrade, '_prepare_v4_bridge', return_value=None):
            result = case.bundle_upgrade(root, commit)
            self.assertEqual(result['status'], 'active_upgrade_complete', result)
            self.assertEqual(J.inspect_journal(case.journal_path)['selected']['sourceCommit'], commit)

    def test_v5_persisted_reader_windows_and_worker_metadata(self):
        import laya_reader_open as opens
        import laya_worker_journal as worker
        import laya_writer_open_plan as plans
        import laya_writer_scope as writers
        from laya_index_sync import sync_owner
        case = self.case_from('laya-reader-move-seal-checks.py', 'MoveSealChecks')
        project = case.case
        selected = case.reader.read_selection(project.journal)
        manifest = case.reader._lock_dir(case.install, create=False) / 'component-windows-v1.json'
        before_manifest = manifest.read_bytes()
        with case.authority(), simulated_reboot():
            with case.lease.resolve_endpoint(project.project, action='recall', host_root=project.plugin) as endpoint:
                self.assertEqual(endpoint.endpoint_id, selected.preferred_endpoint)
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 20) as snapshot:
                    bodies = snapshot.connections[0].execute('SELECT body FROM content').fetchall()
                    self.assertIn('otter', str(bodies))
            self.assertEqual(worker._metadata(project.project)['ledger']['phase'], 'active')
            plan = plans.capture_writer_plan(project.project, executing_root=project.plugin)
            with sync_owner(plan.queue_path) as owner:
                self.assertIsNotNone(owner)
                with writers.normal_writer_scope(plan, queue_owner=owner, deadline=time.monotonic() + 20) as writer:
                    writers.validate_normal_writer(writer)
            self.assertEqual(manifest.read_bytes(), before_manifest)
            self.assertEqual(case.reader.read_selection(project.journal).preferred_endpoint,
                             selected.preferred_endpoint)

    def test_bound_catalog_queries_and_scan_resume(self):
        import laya_source_catalog as source
        case = self.case_from('laya-source-candidate-checks.py', 'CandidateCase')
        catalog, binding = case.basic()
        snapshot = catalog.require_complete()
        scan_id = snapshot.scan_id
        with simulated_reboot():
            rows = case.query(binding, 'alpha')
            self.assertTrue(rows)
            self.assertNotIn('crosses_device', str(rows))
            self.assertTrue(source._node_text_matches(source._node_text(os.stat(case.plans)), os.stat(case.plans)))
            # The persisted six-field signatures retain their format and scan ID.
            catalog.verify_unchanged(snapshot)
            self.assertEqual(catalog.require_complete().scan_id, scan_id)

    def test_schema3_persisted_reader_and_subsequent_publication(self):
        case = self.case_from('laya-journal-schema3-checks.py', 'Schema3EndpointChecks')
        _initial, _target, selected = case.switched()
        with simulated_reboot():
            with case.resolved() as endpoint:
                self.assertEqual(endpoint.endpoint_id, selected.preferred_endpoint)
                self.assertTrue(case.query(endpoint).rows)
            # Ingest and capture reuse the existing worktree binding.
            with case.writer() as store:
                handle = store.capture_view(case.worktree_id)
                with store._session() as db:
                    path = db.execute('SELECT path FROM wiki_members LIMIT 1').fetchone()[0]
                self.assertTrue(store.ingest_card(case.worktree_id, path))
                self.assertIsNotNone(store.head(handle)['gen_id'])

    def test_schema3_generation_publication_retains_binding_bytes(self):
        case = self.case_from('laya-view-generations-checks.py', 'GenerationCase')
        store, repository, handle = case.seeded()
        manifest = case.snapshot(store, handle)
        worktree = handle.worktree_id
        before = store._db.execute('SELECT binding_json,binding_digest,record_json FROM worktrees').fetchall()
        store.close()
        with simulated_reboot():
            reopened = case.reopen(repository)
            handle = reopened.capture_view(worktree)
            head = reopened.head(handle)
            generation = reopened.prepare_generation(handle, manifest, expected_head=head)
            reopened.publish_generation(handle, generation, expected_head=head)
            self.assertEqual(reopened.head(handle)['gen_id'], generation)
            self.assertEqual(reopened._db.execute(
                'SELECT binding_json,binding_digest,record_json FROM worktrees').fetchall(), before)

    def schema3_case(self):
        return self.case_from('laya-journal-schema3-checks.py', 'Schema3EndpointChecks')

    def stored_record(self, case, endpoint_id):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(case.case.journal)) as db:
            return json.loads(db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',
                                         (endpoint_id,)).fetchone()[0])

    def switch_after_reboot(self, case):
        """A v6 reader sealed before the reboot; the schema3 target and the CAS after it."""
        import laya_reader_lease as lease
        case.publish()
        initial = case.reader.read_selection(case.case.journal)
        v6 = self.stored_record(case, initial.preferred_endpoint)
        with case.reader._readonly(case.case.journal, case.case.state) as db:
            before = lease._image(db)
        with simulated_reboot():
            target = case.seal()
            loaded = case.loaded(initial)
            with case.receipt():
                selected = case.cas(target, loaded, base=initial)
            self.assertEqual(case.reader.read_selection(case.case.journal), selected)
            with case.reader._readonly(case.case.journal, case.case.state) as db:
                lease._forward_image(before, lease._image(db))
        return initial, v6, target, selected

    def test_schema3_switch_over_a_v6_reader_sealed_before_the_reboot(self):
        case = self.schema3_case()
        initial, v6, target, selected = self.switch_after_reboot(case)
        self.assertEqual((selected.epoch, selected.preferred_endpoint, selected.previous_endpoint),
                         (initial.epoch + 1, target.endpoint_id, initial.preferred_endpoint))
        # Stored records keep the device each was sealed with; only the inode matches.
        record = self.stored_record(case, target.endpoint_id)
        for key in ('project_node', 'state_node'):
            self.assertEqual(record[key][1], v6[key][1])
            self.assertEqual(record[key][0], v6[key][0] + 1)
        self.assertEqual(self.stored_record(case, initial.preferred_endpoint), v6)
        # Back in the original boot the selection still reads.
        self.assertEqual(case.reader.read_selection(case.case.journal), selected)

    def test_rollback_from_a_schema3_reader_sealed_after_the_reboot(self):
        case = self.schema3_case()
        initial, v6, _target, selected = self.switch_after_reboot(case)
        with simulated_reboot(), case.seed.authority():
            installer = case.reader.seal_current_reader(case.case.state, case.case.project,
                                                        executing_root=case.case.plugin)
            # Re-issued from this boot: the store nodes carry this boot's device.
            reissued = dict(installer.endpoint.record)
            for key in ('db_node', 'catalog_node', 'binding_node'):
                self.assertEqual(reissued[key][1], v6[key][1])
            back = case.reader.cas_reader(case.case.journal, expected_epoch=selected.epoch,
                expected_endpoint=selected.preferred_endpoint, target=installer.endpoint,
                transition_id='2' * 32)
            self.assertEqual(case.reader.read_selection(case.case.journal), back)
        self.assertEqual((back.preferred_endpoint, back.previous_endpoint),
                         (installer.endpoint.endpoint_id, selected.preferred_endpoint))
        self.assertEqual(self.stored_record(case, initial.preferred_endpoint), v6)

    def test_operator_rollback_after_a_reboot_reissues_the_v6_reader(self):
        """The v6 and schema3 readers were sealed before the reboot; the
        rollback re-issues the v6 reader from this boot and checks its store
        nodes against the v6 reader the switch left."""
        # The rollback path imports these lazily; imported first under the
        # fixture's bundle authority they would keep its patched bindings.
        for name in ('laya_active_upgrade', 'laya_bound_upgrade', 'laya_inactive_upgrade'):
            importlib.import_module(name)
        rollback = self.case_from('laya-shadow-rollback-checks.py', 'ShadowRollbackChecks')
        initial, switched = rollback.switched()
        with simulated_reboot():
            result, wakes = rollback.rollback()
        self.assertEqual(result['status'], 'rolled_back', result)
        after = rollback.u15.selection()
        self.assertEqual((after[0], after[2]), (switched[0] + 1, switched[1]))
        self.assertEqual(rollback.u15.store_format(), 'v6')
        self.assertEqual([a[:1] for a, _k in wakes], [('laya-sync',)])

    def test_record_nodes_still_differ_by_inode_and_in_process_owners_keep_the_device(self):
        import laya_move_read_seal as seal
        reader = importlib.import_module('laya_journal_reader')
        base = {'project_node': [4, 8], 'state_node': [4, 9], 'db_node': [4, 10], 'scope': 's'}
        keys = tuple(base)
        self.assertTrue(reader.records_agree(base, {**base, 'project_node': [5, 8], 'state_node': [5, 9],
                                                    'db_node': [5, 10]}, keys))
        for key in ('project_node', 'state_node', 'db_node'):
            with self.subTest(field=key):
                self.assertFalse(reader.records_agree(base, {**base, key: [4, 7]}, keys))
        self.assertFalse(reader.records_agree(base, {**base, 'scope': 't'}, keys))
        # A writer scope compares its own earlier observation in this process,
        # device included: a device change under it refuses.
        case = self.schema3_case()
        with ExitStack() as stack:
            seal._register_writer(stack, case.case.project, case.case.state, kind='reboot-fixture')
            self.assertIsNotNone(seal._owner())
            with simulated_reboot():
                with self.assertRaisesRegex(ValueError, 'reader_move_owner_changed'):
                    seal._owner()

    def test_inference_manifest_cold_and_warm_identity(self):
        case = self.case_from('context-learning-inference-identity-checks.py', 'IdentityChecks')
        raw = (case.state / 'laya-inference-manifest.json').read_bytes()
        with simulated_reboot():
            cold = case.validate()
            self.assertEqual(case.validate(), cold)
            self.assertEqual((case.state / 'laya-inference-manifest.json').read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
