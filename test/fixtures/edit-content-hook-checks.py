import json
import fcntl
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))
import edit_context
import posttool
import preflight_gate
from context_learning import seam
from context_learning import edit_ticket
from context_learning import resident
from context_learning import shared as shared_module
from context_learning.edit_segments import split_with_offsets
from context_learning.contracts import request, digest, validate_request
from context_learning.compact_input import derive
from context_learning.shared import _db
from context_learning.store import canonical
from context_learning.laya_adapter import prediction_batches
from context_learning.synthetic_experiment import PLANNED_BUNDLE_NAME, SHARED_NAME


class EditContentHookChecks(unittest.TestCase):
    def test_scope_settlement_defers_before_gold_and_recovers_only_unreserved_edits(self):
        from context_learning import synthetic_scope, synthetic_teacher, synthetic_experiment
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            project = Path(temp) / 'novel'
            project.mkdir()
            root = project / SHARED_NAME
            root.mkdir(mode=0o700)
            rid = 'temporary-edit-case'
            sample_sha = digest('{}')
            with _db(root) as db:
                db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('key', str(project), rid, sample_sha, '{}', '{}',
                            'b' * 64, time.time(), 'train', '[]', 'active', 'queued'))
                db.execute('INSERT INTO edit_tickets (ticket_id,project_root,story_root,relative_path,episode,approval_sha256,parent_approval_sha256,document_sha256,source_side,source_start,segment_start,segment_end,segment_sha256,event_json,session_id,turn_id,tool_use_id,operation,created_at,expires_at,request_id,input_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('ticket', str(project), str(project),
                            '01_Settings/smoke.md', 0, 'a' * 64, 'b' * 64,
                            'c' * 64, 'after', 0, 0, 1, 'd' * 64, '{}',
                            'session', 'turn', 'tool', 'write', time.time(),
                            time.time() + 60, rid, sample_sha))
            with patch.object(synthetic_scope, '_synthetic_bundle', return_value=(root, project)), patch.object(
                    synthetic_teacher, 'policy_for', return_value={
                        'projectRoot': str(project), 'maxCalls': 2,
                        'reservedUsd': None, 'reserveUsdPerCall': None,
                        'model': 'gpt-6.1-sol'}), patch.object(
                    synthetic_experiment, 'load', return_value=None), patch.object(
                    synthetic_teacher, 'resource_report', return_value={'ready': True}), patch.object(
                    shared_module, '_case_active', return_value=True), patch.object(
                    synthetic_teacher, '_case_active', return_value=True), patch.object(
                    synthetic_teacher, 'allowed_case', side_effect=ValueError(
                        'synthetic_collection_unproven')):
                result = synthetic_teacher.process_pending(root, project, {
                    'maxGoldCalls': 2, 'goldModel': 'gpt-6.1-sol'},
                    invoke=lambda *_a: self.fail('gold called before settlement'))
                self.assertEqual(result['status'], 'deferred_scope_settlement')
                with _db(root) as db:
                    self.assertEqual(db.execute('SELECT status FROM cases').fetchone(),
                                     ('queued',))
                    db.execute("UPDATE cases SET status='pending_review',error='synthetic_collection_unproven'")
                self.assertEqual(shared_module.recover_scope_deferred_edits(
                    root, project), 1)
                with _db(root) as db:
                    self.assertEqual(db.execute('SELECT status,error FROM cases').fetchone(),
                                     ('queued', None))
                    db.execute("UPDATE cases SET status='pending_review',error='synthetic_collection_unproven'")
                    db.execute('INSERT INTO experiment_calls VALUES (?,?,?,?)',
                               ('gold:' + rid + ':first_running', 'gold', 0,
                                time.time()))
                self.assertEqual(shared_module.recover_scope_deferred_edits(
                    root, project), 0)

    def test_post_index_wake_requires_exact_owner_pending_edit_and_waits_for_lock(self):
        from context_learning import synthetic_scope
        import config
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            novel = Path(temp) / 'novel'
            novel.mkdir(mode=0o700)
            shared = novel / SHARED_NAME
            shared.mkdir(mode=0o700)
            other = Path(temp) / 'other'
            other.mkdir()
            cfg = {'sharedLearning': {'enabled': True, 'root': str(shared)}}
            with _db(shared) as db:
                db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('key', str(novel), 'edit-rid', 'a' * 64, '{}', '{}',
                            'b' * 64, time.time(), 'train', '[]', 'active', 'queued'))
                db.execute('INSERT INTO edit_tickets (ticket_id,project_root,story_root,relative_path,episode,approval_sha256,parent_approval_sha256,document_sha256,source_side,source_start,segment_start,segment_end,segment_sha256,event_json,session_id,turn_id,tool_use_id,operation,created_at,expires_at,request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('ticket', str(novel), str(novel), '04_Manuscript/ep001.md',
                            1, 'a' * 64, 'b' * 64, 'c' * 64, 'after', 0, 0, 1,
                            'd' * 64, '{}', 'session', 'turn', 'tool', 'edit',
                            time.time(), time.time() + 60, 'edit-rid'))
            with patch.object(synthetic_scope, 'NOVEL_ROOT', novel), patch.object(
                    synthetic_scope, '_synthetic_bundle', return_value=(shared, novel)), patch.object(
                    synthetic_scope, 'policy_for', return_value={}), patch.object(
                    config, 'find_project_config', return_value={'config': cfg}), patch.object(
                    shared_module, '_safe_root', return_value=shared), patch.object(
                    shared_module, '_policy', return_value={}), patch.object(
                    shared_module, '_case_active', return_value=True), patch.object(
                    shared_module, 'launch', return_value={'status': 'launched'}) as launched:
                self.assertEqual(shared_module.wake_after_index(other)['status'], 'ignored')
                launched.assert_not_called()
                self.assertEqual(shared_module.wake_after_index(novel)['status'], 'launched')
                launched.assert_called_once_with(cfg, novel, after_index=True)
                with _db(shared) as db:
                    db.execute("UPDATE cases SET status='awaiting_local'")
                self.assertEqual(shared_module.wake_after_index(novel)['status'], 'launched')
                with _db(shared) as db:
                    self.assertEqual(db.execute('SELECT status FROM cases').fetchone(),
                                     ('queued',))
                with _db(shared) as db:
                    db.execute("UPDATE cases SET status='gold'")
                self.assertEqual(shared_module.wake_after_index(novel)['status'], 'ignored')
            # A PostToolUse worker may still own the lock after QMD's ACK.
            # The post-index worker must wait, then run with the settled index.
            import threading
            lock_path = shared / 'shared-worker.lock'
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o600)
            held = threading.Event()
            release = threading.Event()
            def holder():
                with lock_path.open('w+') as file:
                    fcntl.flock(file, fcntl.LOCK_EX)
                    held.set()
                    release.wait(2)
                    fcntl.flock(file, fcntl.LOCK_UN)
            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(held.wait(2))
            timer = threading.Timer(.15, release.set)
            timer.start()
            began = time.monotonic()
            from context_learning import synthetic_experiment
            with patch.object(shared_module, '_policy', return_value={}), patch.object(
                    synthetic_experiment, 'load', return_value=({}, 'a' * 64)), patch.object(
                    synthetic_experiment, 'check_disk', return_value=None), patch.object(
                    shared_module, 'observe_trial', return_value={'status': 'waiting'}), patch.object(
                    shared_module, 'train_due', return_value={'status': 'waiting'}):
                result = shared_module._run_impl(shared, wait_for_lock=True)
            thread.join(2)
            timer.join(2)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(time.monotonic() - began, .12)
            self.assertEqual(result['status'], 'waiting')

    def test_awaiting_edit_recovers_before_global_gold_policy(self):
        from context_learning import synthetic_scope, synthetic_teacher
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            root = Path(temp)
            order = []
            def recover(*_args):
                order.append('recover')
            def gate(*_args):
                self.assertEqual(order, ['recover'])
                raise ValueError('stop_after_recovery')
            with patch.object(synthetic_scope, '_synthetic_bundle', return_value=(root, root)), patch.object(
                    synthetic_teacher, 'recover_awaiting_edits', side_effect=recover), patch.object(
                    synthetic_teacher, 'policy_for', side_effect=gate):
                with self.assertRaisesRegex(ValueError, 'stop_after_recovery'):
                    synthetic_teacher.process_pending(root, root, {})
            order.clear()
            with patch.object(shared_module, '_policy', return_value={}), patch.object(
                    synthetic_scope, '_synthetic_bundle', return_value=(root, root)), patch.object(
                    shared_module, 'recover_awaiting_edits', side_effect=recover), patch.object(
                    synthetic_scope, 'policy_for', side_effect=gate), patch.object(
                    shared_module, 'observe_trial', return_value={'status': 'waiting'}), patch.object(
                    shared_module, 'train_due', return_value={'status': 'waiting'}):
                result = shared_module._run_impl(root, project=root)
            self.assertEqual(result['teacher']['reason'], 'stop_after_recovery')

    def test_edit_scope_waits_for_qmd_update_and_delete_settlement(self):
        import sqlite3
        from context_learning import synthetic_scope
        import qmd_route
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            project = Path(temp)
            story = project / PLANNED_BUNDLE_NAME
            raw = story / '04_Manuscript/ep028.md'
            raw.parent.mkdir(parents=True)
            raw.write_text('new synthetic revision')
            old_sha, new_sha = digest('old synthetic revision'), digest(raw.read_text())
            index = project / 'index.sqlite'
            with sqlite3.connect(index) as db:
                db.execute('CREATE TABLE documents (collection TEXT,path TEXT,hash TEXT,active INTEGER)')
                db.execute('INSERT INTO documents VALUES (?,?,?,1)',
                           ('fantasy-20261008-manuscript', 'ep028.md', old_sha))
            selected = {'selected': True, 'INDEX_PATH': str(index)}
            edited = {'04_Manuscript/ep028.md': (old_sha, new_sha)}
            with patch.object(qmd_route, 'project_paths', return_value=selected):
                with self.assertRaisesRegex(ValueError, 'synthetic_index_edit_not_settled'):
                    synthetic_scope._indexed_collections_only(project, project,
                                                               edits=edited)
                with sqlite3.connect(index) as db:
                    db.execute('UPDATE documents SET hash=?', (new_sha,))
                synthetic_scope._indexed_collections_only(project, project,
                                                          edits=edited)
                with sqlite3.connect(index) as db:
                    db.execute('UPDATE documents SET active=0')
                with self.assertRaisesRegex(ValueError, 'synthetic_index_edit_not_settled'):
                    synthetic_scope._indexed_collections_only(project, project,
                                                               edits=edited)
                with sqlite3.connect(index) as db:
                    db.execute('UPDATE documents SET active=1')
                raw.unlink()
                deleted = {'04_Manuscript/ep028.md': (new_sha, None)}
                with self.assertRaisesRegex(ValueError, 'synthetic_index_edit_not_settled'):
                    synthetic_scope._indexed_collections_only(project, project,
                                                               edits=deleted)
                with sqlite3.connect(index) as db:
                    db.execute('UPDATE documents SET active=0')
                synthetic_scope._indexed_collections_only(project, project,
                                                          edits=deleted)

    def test_claude_tool_identity_binds_without_turn_id(self):
        claude_pre = {'session_id': 'claude-session',
                      'tool_use_id': 'tool-42', 'hook_event_name': 'PreToolUse'}
        claude_post = {**claude_pre, 'hook_event_name': 'PostToolUse'}
        self.assertEqual(edit_context._identity(claude_pre),
                         edit_context._identity(claude_post))
        self.assertEqual(edit_context._host_ids(claude_pre),
                         ('claude-session', 'no-turn', 'tool-42'))
        self.assertNotEqual(edit_context._identity(claude_pre),
                            edit_context._identity({**claude_post,
                                                    'tool_use_id': 'tool-43'}))

    def test_multilingual_train_gate_excludes_earlier_query_gold(self):
        from context_learning import laya_adapter
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            root = Path(temp)
            for version, rid in ((3, 'old-query'), (4, 'new-edit')):
                sample = canonical({'schema_version': version,
                                    'prompt': {'text': 'synthetic distinct question ' + rid}})
                with _db(root) as db:
                    db.execute("INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status,gold) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               ('project', str(root), rid, digest(sample), sample,
                                '{}', 'corpus', time.time(), 'train', '[]',
                                'active', 'gold', '{}'))
            policy = {'batchMin': 2, 'batchMax': 5,
                      'trainerArgv': ['/fake/python', '/fake/adapter', '/fake/model']}
            with patch.object(shared_module, 'resource_report', return_value={'ready': True}), patch.object(
                    shared_module, '_reconcile_cases', return_value=0):
                self.assertEqual(shared_module.train_due(root, policy)['status'],
                                 'multilingual_migration_required')
                policy['modelInputView'] = laya_adapter.EDIT_INPUT_VIEW
                result = shared_module.train_due(root, policy)
                self.assertEqual(result['status'], 'waiting')
                self.assertEqual(result['new_gold'], 1)

    def test_multilingual_migration_rejects_active_shared_worker(self):
        from context_learning import laya_adapter
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            root = Path(temp)
            policy = {'modelInputView': laya_adapter.EDIT_INPUT_VIEW,
                      'trainerArgv': ['/fake/python', '/fake/adapter', '/fake/model'],
                      'baseArtifact': str(root / 'base.artifact')}
            (root / 'base.artifact').write_text('{}')
            (root / 'base.artifact').chmod(0o600)
            lease = os.open(root / 'shared-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(shared_module, '_policy', return_value=policy), patch.object(
                        laya_adapter, 'model_identity', return_value='b' * 64), patch.object(
                        laya_adapter, '_head_path', return_value=None):
                    with self.assertRaisesRegex(ValueError, 'multilingual_worker_busy'):
                        shared_module.prepare_multilingual_trial(root,
                            expected_old_pointer_sha256=None)
                self.assertFalse((root / 'multilingual-migration.json').exists())
            finally:
                os.close(lease)

    def test_multilingual_trial_archives_english_pointer_and_recovers_midway(self):
        from context_learning import laya_adapter
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            for name, preexisting_archive in (('first', False), ('resume', True)):
                root = Path(temp) / name
                root.mkdir(mode=0o700)
                artifact = root / 'base.artifact'
                artifact.write_text('{}')
                artifact.chmod(0o600)
                pointer = root / 'experimental-checkpoint.json'
                old_bytes = b'{"model_input_view":"full-candidate-1024-v1","experimental":true}\n'
                pointer.write_bytes(old_bytes)
                pointer.chmod(0o600)
                if preexisting_archive:
                    archive = root / 'english-rollback-checkpoint.json'
                    archive.write_bytes(old_bytes)
                    archive.chmod(0o600)
                policy = {'modelInputView': laya_adapter.EDIT_INPUT_VIEW,
                          'trainerArgv': ['/fake/python', '/fake/adapter', '/fake/model'],
                          'baseArtifact': str(artifact)}
                with patch.object(shared_module, '_policy', return_value=policy), patch.object(
                        laya_adapter, 'model_identity', return_value='b' * 64), patch.object(
                        laya_adapter, '_head_path', return_value=None):
                    result = shared_module.prepare_multilingual_trial(root,
                        expected_old_pointer_sha256=__import__('hashlib').sha256(
                            old_bytes).hexdigest())
                    self.assertEqual(result['status'], 'prepared')
                    self.assertFalse(pointer.exists())
                    self.assertEqual((root / 'english-rollback-checkpoint.json').read_bytes(),
                                     old_bytes)
                    self.assertEqual(shared_module.prepare_multilingual_trial(root,
                        expected_old_pointer_sha256=result['old_pointer_sha256'])['status'],
                        'already_prepared')

    def test_resident_reclaims_stale_socket_only_after_kernel_lease_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            state.chmod(0o700)
            address = state / resident.SOCKET_NAME
            with socket.socket(socket.AF_UNIX) as listener:
                try:
                    listener.bind(str(address))
                except PermissionError:
                    self.skipTest('sandbox denies AF_UNIX bind')
            address.chmod(0o600)
            pid = state / resident.PID_NAME
            pid.write_text('{}')
            pid.chmod(0o600)
            lease = os.open(state / resident.LEASE_NAME,
                            os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, 'resident_busy_or_starting'):
                    resident._launch(state, '/fake/python', '/fake/model',
                                     state / 'artifact', 'a' * 64,
                                     'tool-edit-8192-choice-v1',
                                     time.monotonic())
                self.assertTrue(address.exists())
                fcntl.flock(lease, fcntl.LOCK_UN)
                with patch.object(resident.subprocess, 'Popen'):
                    with self.assertRaisesRegex(ValueError, 'resident_start_timeout'):
                        resident._launch(state, '/fake/python', '/fake/model',
                                         state / 'artifact', 'a' * 64,
                                         'tool-edit-8192-choice-v1',
                                         time.monotonic())
                self.assertFalse(address.exists())
                self.assertFalse(pid.exists())
            finally:
                os.close(lease)

    def test_resident_socket_protocol_is_bounded_json(self):
        left, right = socket.socketpair()
        try:
            resident._write(left, {'schema': 1, 'body': '가상'})
            self.assertEqual(resident._read(right), {'schema': 1, 'body': '가상'})
            left.sendall(b'not-json\n')
            with self.assertRaises(ValueError):
                resident._read(right)
            left.sendall(b'{')
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, 'resident_handshake_timeout'):
                resident._read(right, deadline=started + .03)
            self.assertLess(time.monotonic() - started, .12)
        finally:
            left.close()
            right.close()

    def test_resident_protocol_upgrade_only_stops_known_idle_old_worker(self):
        class ReplySocket:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def settimeout(self, _value): pass
            def connect(self, _path): pass
        path = Path('/tmp/owner-private-resident.sock')
        with patch.object(resident.socket, 'socket', return_value=ReplySocket()), \
                patch.object(resident, '_write') as sent, \
                patch.object(resident, '_read', return_value={
                    'status': 'ready', 'protocol': resident.WIRE_PROTOCOL}):
            self.assertTrue(resident._protocol_compatible(path))
            sent.assert_called_once_with(sent.call_args.args[0],
                                         {'command': 'version'})
        with patch.object(resident.socket, 'socket', return_value=ReplySocket()), \
                patch.object(resident, '_write'), \
                patch.object(resident, '_read', return_value={
                    'status': 'worker_error', 'prediction': None,
                    'timing': {'worker_elapsed_seconds': 0.001}}):
            self.assertFalse(resident._protocol_compatible(path))
        with patch.object(resident.socket, 'socket', return_value=ReplySocket()), \
                patch.object(resident, '_write'), \
                patch.object(resident, '_read', side_effect=socket.timeout()):
            self.assertIsNone(resident._protocol_compatible(path))

    def test_resident_peer_deadline_and_disconnect_are_checked_before_scoring(self):
        left, right = socket.socketpair()
        try:
            self.assertTrue(resident._same_owner(left))
            epoch = 1700000000.0
            worker_monotonic = 100.0
            envelope = {'queued_at_epoch': epoch - .25,
                        'deadline_epoch': epoch + 1}
            deadline, waited = resident._request_window(envelope,
                now_epoch=epoch, now_monotonic=worker_monotonic)
            self.assertEqual(deadline, worker_monotonic + 1)
            self.assertGreaterEqual(waited, .25)
            left.settimeout(.05)
            checked = time.monotonic()
            live_deadline = checked + 1
            self.assertFalse(resident._cancelled(left, live_deadline))
            self.assertLess(time.monotonic() - checked, .04)
            with self.assertRaisesRegex(ValueError, 'resident_deadline_expired'):
                resident._request_window(envelope, now_epoch=epoch + 2,
                                         now_monotonic=worker_monotonic + 2)
            with self.assertRaisesRegex(ValueError, 'resident_wire_invalid'):
                resident._request_window({**envelope,
                    'queued_at_epoch': epoch + 10}, now_epoch=epoch,
                    now_monotonic=worker_monotonic)
            # A sender on a different monotonic origin still gets the same
            # remaining interval after the worker receives the request.
            shifted, _ = resident._request_window(envelope,
                now_epoch=epoch + .2, now_monotonic=1462055.0)
            self.assertAlmostEqual(shifted, 1462055.8)
            self.assertEqual(resident._failure_code(ValueError(
                'model_token_budget_exceeded')), 'input_budget_exceeded')
            self.assertEqual(resident._failure_code(ValueError(
                '/private/story/source.md')), 'worker_error')
            right.close()
            self.assertTrue(resident._cancelled(left, live_deadline))
        finally:
            left.close()
            right.close()

    def test_loaded_predict_skips_tokenization_for_cancelled_turn(self):
        from context_learning import laya_adapter
        with patch.dict(sys.modules, {'torch': __import__('types').ModuleType('torch')}), patch.object(
                laya_adapter, '_pairs', side_effect=AssertionError('stale work ran')):
            with self.assertRaisesRegex(ValueError, 'prediction_cancelled'):
                laya_adapter.predict_loaded(None, None,
                    {'model_input_view': laya_adapter.EDIT_INPUT_VIEW},
                    {'schema_version': 1,
                     'model_input_view': laya_adapter.EDIT_INPUT_VIEW},
                    cancelled=lambda: True)

    def test_pair_tokenization_stops_between_candidates_after_cancel(self):
        from context_learning import laya_adapter
        done = []
        with patch.object(laya_adapter, '_pair',
                          side_effect=lambda *_args: done.append(1) or {'ids': [1]}):
            with self.assertRaisesRegex(ValueError, 'prediction_cancelled'):
                laya_adapter._pairs([{'request_id': 'synthetic',
                    'prompt': 'synthetic edit',
                    'candidates': [{'id': 'one'}, {'id': 'two'}]}],
                    None, {}, with_gold=False,
                    cancelled=lambda: bool(done))
        self.assertEqual(len(done), 1)
        self.assertLessEqual(resident.HANDSHAKE_SECONDS, .5)

    def test_resident_lock_rejects_nonregular_or_public_file(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            public = state / 'public.lock'
            public.write_text('')
            public.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'resident_lock_unsafe'):
                resident._private_lock(public)
            private = state / 'private.lock'
            descriptor = resident._private_lock(private)
            os.close(descriptor)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)

    def test_resident_admission_simulates_constrained_device_without_system_load(self):
        def probe(available, free_percent=40, ready=True):
            return lambda _policy, detailed: {'ready': ready,
                'available_mib': available,
                'memory_pressure': {'system_free_percent': free_percent}}
        limits, _ = resident._admission(4096, already_running=False,
                                       probe=probe(3000))
        self.assertIsNone(limits)  # No new multi-GB worker under pressure.
        limits, _ = resident._admission(4096, already_running=True,
                                       probe=probe(1500))
        self.assertEqual(limits, {'max_batch_size': 1,
                                  'max_padded_tokens': 4096})
        limits, _ = resident._admission(4096, already_running=True,
                                       probe=probe(3000))
        self.assertEqual(limits['max_batch_size'], 2)
        limits, _ = resident._admission(4096, already_running=True,
                                       probe=probe(6000))
        self.assertEqual(limits, {'max_batch_size': 4,
                                  'max_padded_tokens': 8192})
        self.assertIsNone(resident._admission(4096, already_running=True,
            probe=probe(6000, free_percent=5))[0])

    def test_resident_client_timeout_retains_partial_admission_timing(self):
        class TimeoutClient:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def settimeout(self, _value): pass
            def connect(self, _path): raise socket.timeout()
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            state.chmod(0o700)
            artifact = state / 'base.artifact'
            artifact.write_text('synthetic')
            artifact.chmod(0o600)
            policy = {'trainerArgv': ['/fake/python', '/fake/adapter', '/fake/model'],
                      'maxRssMib': 4096}
            request = {'artifact_path': str(artifact),
                       'model_input_view': 'tool-edit-8192-choice-v1'}
            with patch.object(resident, '_socket_ready', return_value=True), patch.object(
                    resident, '_protocol_compatible', return_value=True), patch.object(
                    resident.socket, 'socket', return_value=TimeoutClient()):
                with self.assertRaises(resident.ResidentFailure) as caught:
                    resident.predict(state, policy, request,
                        resident._hash(artifact), deadline=time.monotonic() + 5,
                        probe=lambda _policy, detailed: {'ready': True,
                            'available_mib': 6000, 'memory_pressure': None})
            self.assertEqual(str(caught.exception), 'resident_deadline_expired')
            self.assertEqual(caught.exception.timing['admission_available_mib'], 6000)
            self.assertIn('rerank_wall_seconds', caught.exception.timing)

    def test_host_edit_ticket_binds_once_to_exact_segment_and_candidate_revision(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            project = Path(temp)
            story = project / PLANNED_BUNDLE_NAME
            draft = story / '04_Manuscript/ep028.md'
            draft.parent.mkdir(parents=True)
            before = '합성 규칙을 유지한다. 옛 내용은 사라진다. 끝.'
            after = '합성 규칙을 유지한다. 끝.'
            draft.write_text(after)
            wiki = story / '.auto-context/wiki/doc.md'
            wiki.parent.mkdir(parents=True)
            wiki_source = '---\ntitle: Synthetic rule\n---\nA synthetic rule stays active.\n'
            wiki.write_text(wiki_source)
            (project / '.auto-context/wiki').mkdir(parents=True)
            shared = project / SHARED_NAME
            shared.mkdir(mode=0o700)
            region, start, side = edit_context._changed_region(before, after)
            self.assertEqual(side, 'before')
            change = {'operation': 'edit', 'path': '04_Manuscript/ep028.md',
                      'text': region, 'source_start': start, 'source_side': side,
                      'source_sha256': digest(region),
                      'document_sha256': digest(before),
                      'before_sha256': digest(before), 'after_sha256': digest(after)}
            event = {'edit_session_id': 'session', 'edit_turn_id': 'turn',
                     'edit_tool_use_id': 'tool'}
            metadata = {'parent_sha256': digest(before),
                        'path_sha256': digest(change['path']),
                        'segment_sha256': digest(region),
                        'start': start, 'end': start + len(region),
                        'index': 0, 'count': 1, 'change_index': 0,
                        'change_count': 1, 'session_id': 'session',
                        'turn_id': 'turn', 'tool_use_id': 'tool',
                        'operation': 'edit'}
            cid = 'fantasy-20261008-wiki/doc.md'
            revision = digest(wiki_source)
            with _db(shared) as db:
                db.execute('INSERT INTO experiment_generated (kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) VALUES (?,?,?,?,?,?,?)',
                           ('wiki_review', 28, 1, '.auto-context/wiki/doc.md',
                            revision, None, time.time()))
                db.execute('INSERT INTO experiment_generated (kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) VALUES (?,?,?,?,?,?,?)',
                           ('writing', 28, 1, '04_Manuscript/ep028.md',
                            digest(before), None, time.time()))
            with patch.object(edit_ticket, '_approval', return_value=('a' * 64, 'b' * 64)), patch.object(
                    __import__('context_learning.shared', fromlist=['_consent']),
                    '_consent', return_value=True):
                ticket = edit_ticket.reserve(project, change, event, metadata, region)
                sample_event = {**metadata, 'ticket_id': ticket}
                sampling = {'mode': 'eligible-policy-pool',
                    'scope': 'queried-phases-only', 'phase_counts': {'primary': 1},
                    'per_phase_limit': 30, 'eligible_ids': [cid],
                    'captured_ids': [cid], 'baseline_selected_ids': [cid],
                    'excluded': [], 'snapshot_failures': [],
                    'retrieval_truncated': False, 'assessment_complete': True,
                    'complete_within_returned_bound': True,
                    'index_revision': 'c' * 64, 'candidate_limit': 15}
                sample = request({'prompt': region, 'context_origin': 'tool_edit',
                                  'edit_event': sample_event},
                                 [{'candidate_id': cid, 'revision_sha256': revision,
                                   'eligible': True, 'excerpt': wiki_source}],
                                 host='codex', request_id='edit-case-1',
                                 sampling=sampling)
                bodies = {cid: derive(wiki_source, revision)['body_text']}
                with _db(shared) as db:
                    edit_ticket.bind(db, shared, project, sample, bodies,
                                     digest(canonical(sample)), commit=False)
                    edit_ticket.bind(db, shared, project, sample, bodies,
                                     digest(canonical(sample)), commit=True)
                    edit_ticket.validate_case(db, shared, project, sample, bodies)
                    with patch.object(edit_ticket, '_approval',
                                      return_value=('a' * 64, 'c' * 64)):
                        with self.assertRaisesRegex(ValueError, 'edit_ticket_not_bound'):
                            edit_ticket.validate_case(db, shared, project, sample, bodies)
                    with self.assertRaisesRegex(ValueError, 'edit_ticket_missing_used_or_expired'):
                        edit_ticket.bind(db, shared, project, sample, bodies,
                                         digest(canonical(sample)), commit=True)
                    tampered = {**sample, 'edit_event': {**sample_event,
                                'parent_sha256': 'f' * 64}}
                    with self.assertRaises(ValueError):
                        edit_ticket.validate_case(db, shared, project, tampered, bodies)
                draft.write_text(after + '\n다른 내용')
                with _db(shared) as db:
                    with self.assertRaisesRegex(ValueError, 'edit_ticket_not_bound'):
                        edit_ticket.validate_case(db, shared, project, sample, bodies)
                draft.write_text(after)
                next_ticket = edit_ticket.reserve(project, change, event, metadata, region)
                next_sample = request({'prompt': region,
                    'context_origin': 'tool_edit',
                    'edit_event': {**metadata, 'ticket_id': next_ticket}},
                    [{'candidate_id': cid, 'revision_sha256': revision,
                      'eligible': True, 'excerpt': wiki_source}],
                    host='codex', request_id='edit-case-2', sampling=sampling)
                with patch.object(shared_module, '_safe_root', return_value=shared), patch.object(
                        shared_module, '_policy', return_value={'goldModel': 'gpt-6.1-sol'}), patch.object(
                        __import__('context_learning.synthetic_experiment', fromlist=['load']),
                        'load', return_value=({'synthetic': True}, 'c' * 64)):
                    self.assertEqual(shared_module.enqueue({}, project, next_sample,
                                     {cid: wiki_source}), 'awaiting_local')
                with _db(shared) as db:
                    self.assertEqual(db.execute('SELECT request_id FROM edit_tickets WHERE ticket_id=?',
                                                (next_ticket,)).fetchone()[0], 'edit-case-2')
                    self.assertEqual(db.execute('SELECT status FROM cases WHERE request_id=?',
                                                ('edit-case-2',)).fetchone()[0], 'awaiting_local')
                from context_learning.store import capture
                local_state = project / '.private'
                local_state.mkdir(mode=0o700)
                cfg = {'contextLearning': {'stateRoot': str(local_state)},
                       'collectionPaths': {cid: str(Path(story.name) / '.auto-context/wiki')}}
                self.assertEqual(capture(next_sample, enabled=True,
                    state_dir=seam._private_state(cfg, project),
                    compact_sources={cid: wiki_source}), 'stored')
                with patch.object(shared_module, '_safe_root', return_value=shared), patch.object(
                        shared_module, '_case_active', return_value=True):
                    self.assertEqual(shared_module.activate_edit_case({}, project,
                                     next_sample, captured=True), 'queued')
                    self.assertEqual(shared_module.activate_edit_case({}, project,
                                     next_sample, captured=True), 'edit_activation_conflict')
                from context_learning import synthetic_scope
                with patch.object(shared_module, '_current_config', return_value=cfg):
                    with _db(shared) as db:
                        self.assertEqual(synthetic_scope._active_edit_revisions(
                            db, shared, project, story)['04_Manuscript/ep028.md'],
                            (digest(before), digest(after)))
                    recovery, edits = synthetic_scope._generated_collections_only(
                        shared, project)
                    self.assertEqual(recovery, {})
                    self.assertEqual(edits['04_Manuscript/ep028.md'][1], digest(after))
                from context_learning import synthetic_scope
                with patch.object(synthetic_scope, 'policy_for', return_value={
                        'projectRoot': str(project), 'maxInputBytes': 131072}), patch.object(
                        __import__('context_learning.synthetic_experiment', fromlist=['load']),
                        'load', return_value=({'synthetic': True}, 'c' * 64)):
                    allowed, gold_input = synthetic_scope.allowed_case(
                        shared, project, next_sample, bodies)
                    self.assertEqual(allowed['projectRoot'], str(project))
                    self.assertIn(region, gold_input)
                from context_learning import synthetic_experiment
                with patch.object(synthetic_experiment, 'load', return_value=(
                        {'id': 'synthetic'}, 'c' * 64)), patch.object(
                        synthetic_experiment, 'approval', return_value={
                            'approvedTrainCycles': None}), patch.object(
                        synthetic_experiment, '_limits', return_value=(100, None, None, None)), patch.object(
                        synthetic_experiment, 'check_disk', return_value=None):
                    with _db(shared) as db:
                        synthetic_experiment.reserve_train(db, shared, project,
                                                           'edit-cycle')
                        self.assertEqual(db.execute('SELECT cycle_id FROM experiment_cycles').fetchone(),
                                         ('edit-cycle',))

    def test_small_edit_uses_changed_span_and_keeps_document_revision(self):
        old = '앞쪽 합성 장면. ' * 500 + '옛 규칙' + '뒤쪽 합성 장면. ' * 500
        new = old.replace('옛 규칙', '새 규칙', 1)
        region, offset, side = edit_context._changed_region(old, new)
        self.assertIn('새 규칙', region)
        self.assertEqual(side, 'after')
        self.assertLess(len(region), len(new) // 4)
        self.assertEqual(new[offset:offset + len(region)], region)
        deleted, position, side = edit_context._changed_region(old, new.replace('새 규칙', ''))
        self.assertIn('옛 규칙', deleted)
        self.assertEqual(side, 'before')
        self.assertEqual(old[position:position + len(deleted)], deleted)

    def test_expired_edit_preimages_pruned_and_janitor_failure_removes_new_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.qmd-fantasy-shared-20261008').mkdir(mode=0o700)
            path = edit_context._receipt(root, 'a' * 64)
            path.write_text('{"private":"synthetic"}')
            path.chmod(0o600)
            old = time.time() - edit_context.RECEIPT_TTL_SECONDS - 1
            os.utime(path, (old, old))
            self.assertEqual(edit_context._prune(path.parent), 0)
            self.assertFalse(path.exists())
            with patch.object(edit_context, '_scope', return_value=(root, root, ['ep.md'])), patch.object(
                    edit_context, '_schedule_prune', side_effect=OSError('blocked')), patch.object(
                    edit_context, 'snapshot_beneath', return_value=b'synthetic'):
                payload = {'hook_event_name': 'PreToolUse', 'session_id': 's',
                           'turn_id': 't', 'tool_use_id': 'u'}
                with self.assertRaisesRegex(ValueError, 'edit_hook_janitor_unavailable'):
                    edit_context.before(payload)
                self.assertFalse(edit_context._receipt(root, edit_context._identity(payload)).exists())

    def test_edit_capture_waits_for_single_use_ticket_before_any_store(self):
        with patch.object(seam, '_private_state') as private_state:
            self.assertEqual(seam.observe({'prompt': 'synthetic edit',
                                           'context_origin': 'tool_edit'},
                                          {'contextLearning': {'capture': True}},
                                          '/synthetic', []), 'edit_ticket_required')
        private_state.assert_not_called()

    def test_separate_owner_approval_required_and_parent_digest_pinned(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            root = Path(temp) / 'project'
            story = root / PLANNED_BUNDLE_NAME
            story.mkdir(parents=True)
            parent = Path(temp) / 'parent.json'
            parent.write_text('{}')
            parent.chmod(0o600)
            from context_learning import synthetic_scope
            with patch.dict(os.environ, {'HOME': temp}), patch.object(
                    synthetic_scope, 'APPROVAL_FILE', parent):
                with self.assertRaisesRegex(ValueError, 'edit_approval_missing'):
                    edit_context._approved(root, story)
                folder = Path(temp) / '.config/qmd/real-edit-approval'
                folder.mkdir(parents=True, mode=0o700)
                folder.chmod(0o700)
                approval = folder / 'current.json'
                value = {'schema': 'qmd-real-edit-approval-v1', 'enabled': True,
                         'projectRoot': str(root), 'storyRoot': str(story),
                         'sharedRoot': str(edit_context.shared_for(root)),
                         'allowLocalCapture': True, 'allowExternalGold': True,
                         'parentApprovalSha256':
                         __import__('hashlib').sha256(parent.read_bytes()).hexdigest()}
                approval.write_text(json.dumps(value))
                approval.chmod(0o600)
                self.assertEqual(edit_context._approved(root, story), value)
                parent.write_text('{"changed":true}')
                with self.assertRaisesRegex(ValueError, 'edit_approval_changed'):
                    edit_context._approved(root, story)

    def test_zero_overlap_and_final_window_cap(self):
        body = 'x' * 300
        offsets = [(i, i + 1) for i in range(len(body))]
        rows = split_with_offsets(body, offsets, target_tokens=128,
                                  overlap_tokens=0)
        self.assertEqual([(row['start'], row['end']) for row in rows],
                         [(0, 128), (128, 256), (256, 300)])
        over = 'x' * (65 * 128)
        with self.assertRaisesRegex(ValueError, 'edit_segment_limit'):
            split_with_offsets(over, [(i, i + 1) for i in range(len(over))],
                               target_tokens=128, overlap_tokens=0)

    def test_microbatches_preserve_order_and_bound_padded_tokens(self):
        sizes = [1000, 1700, 2200, 5000, 8000, 1600, 1600, 1600, 1600]
        items = [{'ids': [0] * size, 'ordinal': number}
                 for number, size in enumerate(sizes)]
        groups = prediction_batches(items)
        self.assertEqual([item['ordinal'] for group in groups for item in group],
                         list(range(len(items))))
        self.assertTrue(all(len(group) <= 4 and
                            max(len(item['ids']) for item in group) * len(group) <= 8192
                            for group in groups))
        self.assertTrue(any(len(group) > 1 for group in groups))

    def test_denied_gate_cannot_leave_edit_preimage(self):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch',
                   'cwd': '/synthetic', 'tool_input': {'command': '*** Begin Patch'}}
        with patch.object(preflight_gate.qmd_config, 'load_project_config', return_value={}), patch.object(
                preflight_gate.rp, 'resolve_paths', return_value={'reason': 'pending'}), patch.object(
                preflight_gate, '_snapshot_allowed_edit') as snapshot, patch.object(
                sys, 'stdin', __import__('io').StringIO(json.dumps(payload))), patch.object(
                sys, 'stdout', __import__('io').StringIO()):
            self.assertEqual(preflight_gate.main(), 0)
        snapshot.assert_not_called()

    def test_overlapping_windows_cover_tail_and_dedupe_selected_cards(self):
        text = ('가상의 봉인 규칙과 인물의 선택을 설명하는 문장.\n' * 250)
        offsets = [(i, i + 1) for i in range(len(text))]
        windows = split_with_offsets(text, offsets, target_tokens=128,
                                     overlap_tokens=16)
        self.assertEqual(windows[0]['start'], 0)
        self.assertEqual(windows[-1]['end'], len(text))
        self.assertTrue(all(b['start'] <= a['end']
                            for a, b in zip(windows, windows[1:])))
        seen = []
        def recaller(value):
            seen.append(value)
            index = value['edit_event']['index']
            cid = 'wiki/tail' if index == len(windows) - 1 else 'wiki/common'
            return {'qmdInternalEdit': {'provenance_complete': True, 'selected': [{
                'candidate_id': cid, 'revision_sha256': 'b' * 64,
                'context': '관련 문서:\n- [wiki] /wiki/' + cid + ' - 제목\n  | 요약'}]}}
        event = {'changes': [{'operation': 'write', 'path': '04_Manuscript/ep028.md',
                  'text': text, 'source_sha256': digest(text)}],
                 'edit_session_id': 'session', 'edit_turn_id': 'turn',
                 'edit_tool_use_id': 'tool'}
        ticket = lambda _change, metadata, _segment: f"{metadata['index']:032x}"
        outcome = posttool._edit_flow(event, '/synthetic', 'codex',
                                     segmenter=lambda _: windows, recaller=recaller,
                                     ticket_writer=ticket)
        self.assertTrue(outcome['complete'])
        self.assertEqual(outcome['completed'], len(windows))
        self.assertEqual(outcome['selected_count'], 2)
        self.assertIn('wiki/tail', outcome['context'])
        self.assertIn('요약', outcome['context'])
        self.assertEqual(len(seen), len(windows))
        self.assertEqual(seen[-1]['edit_event']['segment_sha256'], digest(windows[-1]['text']))

        second = {'operation': 'write', 'path': '04_Manuscript/ep029.md',
                  'text': text, 'source_sha256': digest(text)}
        two_file = posttool._edit_flow({**event, 'changes': [event['changes'][0], second]},
            '/synthetic', 'codex', segmenter=lambda _: windows[:2], recaller=recaller,
            ticket_writer=ticket)
        self.assertTrue(two_file['complete'])
        self.assertEqual(two_file['selected_count'], 1)
        self.assertIn('구간 1,2,3,4', two_file['context'])
        self.assertNotEqual(seen[-4]['edit_event']['path_sha256'],
                            seen[-2]['edit_event']['path_sha256'])

        def incomplete(value):
            return None if value['edit_event']['index'] == 2 else recaller(value)
        prepared_windows = []
        outcome = posttool._edit_flow(event, '/synthetic', 'codex',
                                     segmenter=lambda _: windows, recaller=incomplete,
                                     ticket_writer=lambda change, metadata, segment:
                                         (prepared_windows.append(metadata['index']) or
                                          ticket(change, metadata, segment)))
        self.assertFalse(outcome['complete'])
        self.assertEqual(outcome['completed'], 2)
        self.assertIn('부분 2/', outcome['context'])
        self.assertEqual(prepared_windows, list(range(len(windows))))

        def missing_revision(value):
            result = recaller(value)
            result['qmdInternalEdit']['provenance_complete'] = False
            return result
        outcome = posttool._edit_flow(event, '/synthetic', 'codex',
                                     segmenter=lambda _: windows, recaller=missing_revision,
                                     ticket_writer=ticket)
        self.assertFalse(outcome['complete'])
        self.assertEqual(outcome['reason'], 'edit_selection_revision_unavailable')

    def test_tool_edit_contract_keeps_long_segment_and_identity(self):
        text = '가상의 마법 규칙과 인물 결정. ' * 300
        edit = {'ticket_id': 'a' * 32, 'parent_sha256': 'a' * 64, 'segment_sha256': digest(text),
                'path_sha256': digest('04_Manuscript/ep028.md'),
                'change_index': 0, 'change_count': 1,
                'start': 0, 'end': len(text), 'index': 0, 'count': 1,
                'session_id': 's', 'turn_id': 't', 'tool_use_id': 'u',
                'operation': 'write'}
        rows = [{'candidate_id': 'wiki/a', 'revision_sha256': 'b' * 64,
                 'eligible': True, 'excerpt': '가상 위키 카드'}]
        sampling = {'mode': 'eligible-policy-pool', 'scope': 'queried-phases-only',
                    'phase_counts': {'primary': 1}, 'per_phase_limit': 30,
                    'eligible_ids': ['wiki/a'], 'captured_ids': ['wiki/a'],
                    'baseline_selected_ids': ['wiki/a'], 'excluded': [],
                    'snapshot_failures': [], 'retrieval_truncated': False,
                    'assessment_complete': True,
                    'complete_within_returned_bound': True,
                    'index_revision': 'c' * 64, 'candidate_limit': 15}
        sample = request({'prompt': text, 'context_origin': 'tool_edit',
                          'edit_event': edit}, rows, host='codex',
                         request_id='tool-sample', sampling=sampling)
        self.assertEqual(sample['schema_version'], 4)
        self.assertFalse(sample['prompt']['truncated'])
        self.assertGreater(len(sample['prompt']['text']), 2000)
        validate_request(sample)
        # The real PostToolUse path includes this marker for both a pending
        # create/update capture and a delete. Both forms must reach observe's
        # request contract without dropping the authenticated edit event.
        for deferred in (True, False):
            marked = request({'prompt': text, 'context_origin': 'tool_edit',
                              'edit_event': {**edit, 'deferred_capture': deferred}},
                             rows, host='codex', request_id='marked-edit',
                             sampling=sampling)
            self.assertIs(marked['edit_event']['deferred_capture'], deferred)
            validate_request(marked)
        marked['edit_event']['deferred_capture'] = 'true'
        with self.assertRaisesRegex(ValueError, 'invalid_edit_event'):
            validate_request(marked)
        sample['edit_event']['segment_sha256'] = 'd' * 64
        with self.assertRaises(ValueError):
            validate_request(sample)

    def test_real_tool_identity_write_edit_delete_and_long_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            story = root / PLANNED_BUNDLE_NAME
            folder = story / "04_Manuscript"
            folder.mkdir(parents=True)
            (root / ".qmd-fantasy-shared-20261008").mkdir(mode=0o700)
            target = folder / "ep028.md"
            payload = {"cwd": str(story), "hook_event_name": "PreToolUse",
                       "session_id": "session-test", "turn_id": "turn-test",
                       "tool_use_id": "tool-test", "tool_name": "apply_patch",
                       "tool_input": {"command": "*** Begin Patch\n*** Add File: 04_Manuscript/ep028.md\n+test\n*** End Patch"}}
            found = {"projectRoot": str(root), "config": {
                "indexing": True, "sharedLearning": {"enabled": True},
                "contextLearning": {"capture": True}, "collectionPaths": {
                "manuscript": PLANNED_BUNDLE_NAME + "/04_Manuscript"}}}
            with patch.object(edit_context, '_schedule_prune'), patch.object(
                    edit_context, "_approved", return_value=True), patch.object(
                    edit_context, "NOVEL_ROOT", root), patch.object(
                    edit_context.config, "find_project_config", return_value=found):
                edit_context.before(payload)
                body = "도서관의 봉인 규칙과 등장인물의 결정을 확인한다.\n" * 250
                self.assertGreater(len(body), 6000)
                target.write_text(body)
                result = edit_context.after({**payload, "hook_event_name": "PostToolUse"})
                self.assertEqual(result["context_origin"], "tool_edit")
                self.assertIn(body, result["prompt"])
                with self.assertRaises(ValueError):
                    edit_context.after({**payload, "hook_event_name": "PostToolUse"})

                payload["tool_use_id"] = "tool-edit"
                payload["tool_input"]["command"] = "*** Begin Patch\n*** Update File: 04_Manuscript/ep028.md\n@@\n-old\n+new\n*** End Patch"
                edit_context.before(payload)
                changed = body + "\n새로운 장면"
                target.write_text(changed)
                result = edit_context.after({**payload, "hook_event_name": "PostToolUse"})
                self.assertTrue(result["prompt"].startswith("edit "))
                self.assertIn("새로운 장면", result["prompt"])
                self.assertLess(len(result['changes'][0]['text']), len(changed))
                self.assertEqual(result['changes'][0]['document_sha256'],
                                 __import__('hashlib').sha256(changed.encode()).hexdigest())

                payload["tool_use_id"] = "tool-delete"
                payload["tool_input"]["command"] = "*** Begin Patch\n*** Delete File: 04_Manuscript/ep028.md\n*** End Patch"
                edit_context.before(payload)
                target.unlink()
                result = edit_context.after({**payload, "hook_event_name": "PostToolUse"})
                self.assertTrue(result["prompt"].startswith("delete "))
                self.assertIn(changed, result["prompt"])

                found['config']['contextLearning']['capture'] = False
                payload['tool_use_id'] = 'tool-revoked'
                with self.assertRaisesRegex(ValueError, 'edit_hook_not_enrolled'):
                    edit_context.before(payload)

    def test_scope_rejects_other_novel_and_missing_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            story = root / PLANNED_BUNDLE_NAME
            story.mkdir()
            found = {"projectRoot": str(root), "config": {"collectionPaths": {}}}
            payload = {"cwd": str(root / "other-story"), "hook_event_name": "PreToolUse",
                       "tool_input": {"file_path": "04_Manuscript/ep001.md"}}
            with patch.object(edit_context, "_approved", return_value=True), patch.object(
                    edit_context, "NOVEL_ROOT", root), patch.object(
                    edit_context.config, "find_project_config", return_value=found):
                with self.assertRaises(ValueError):
                    edit_context.before(payload)


if __name__ == "__main__":
    unittest.main()
