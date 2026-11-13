#!/usr/bin/env python3
"""Synthetic owner-lock contention through the real laya-compile wake entry."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from memvane.runtime import runtime_wake_policy as policy
from memvane.wiki.workspace import wiki_selected_laya; import memvane.wiki.compile.selected_execution as wiki_selected_laya_selected_execution
from memvane.wiki.compile import wiki_compile_worker
from memvane.kernel.resource_deferred import ResourceDeferred


HOLDER = r'''
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print('held', flush=True)
sys.stdin.read(1)
os.close(fd)
'''

WAKE = r'''
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import laya_product_bundle; import memvane.route.index_host_route as laya_index_host_route; import memvane.journal.route_selection as laya_index_host_route_route_selection; import memvane.app.wake_entry as laya_wake_entry
root, project, queue = sys.argv[1:]
proof = {'sourceCommit': 'a'*40, 'bundleManifestSha256': 'b'*64,
         'ownershipManifestSha256': 'c'*64, 'pluginRoot': root}
decision = {'state': 'candidate', 'projectRoot': project,
            'queuePath': queue, 'journalPath': project + '/journal.sqlite'}
with patch.object(laya_product_bundle, 'inspect_full_plugin_candidate', return_value=proof), \
     patch.object(laya_index_host_route_route_selection, 'select_route', return_value=decision):
    sys.exit(laya_wake_entry.main(['laya-compile', project, root, 'a'*40, 'b'*64, 'c'*64]))
'''


class CompileOwnerBusyChecks(unittest.TestCase):
    def test_wake_entry_real_anchor_contention_returns_deferred(self):
        from memvane.kernel.package import sync_owner_directory
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as temp:
            project = Path(temp).resolve()
            queue = project / 'claims.sqlite'
            queue.touch(mode=0o600)
            lock = sync_owner_directory(create=True) / 'owner.lock'
            holder = subprocess.Popen([sys.executable, '-B', '-c', HOLDER, str(lock)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'held')
                self.assertIsNone(holder.poll(), 'lock holder must be alive')
                child = subprocess.run([sys.executable, '-B', '-c', WAKE, str(ROOT),
                                        str(project), str(queue)],
                                       capture_output=True, text=True, check=False)
            finally:
                holder.communicate(input='x', timeout=30)
            self.assertEqual(holder.returncode, 0)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertNotIn('Traceback', child.stderr)
            self.assertEqual(json.loads(child.stdout), {'state': 'deferred', 'reason': 'owner_busy'})

    def test_compile_worker_main_reports_owner_busy(self):
        output = io.StringIO()
        found = {'projectRoot': os.environ['HOME'], 'config': {}}
        with mock.patch.object(sys, 'argv', ['wiki_compile_worker.py', '--cwd', os.environ['HOME'], '--json']), \
             mock.patch.object(wiki_compile_worker.qmd_config, 'find_project_config', return_value=found), \
             mock.patch.object(wiki_selected_laya, 'selected', side_effect=ResourceDeferred('owner_busy')), \
             redirect_stdout(output):
            result = wiki_compile_worker.main()
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {'state': 'deferred', 'reason': 'owner_busy'})

    def test_parent_preserves_worker_deferred_json(self):
        project = Path(os.environ['HOME']).resolve()
        bound = ({'projectRoot': str(project)}, {})
        child = subprocess.CompletedProcess([], 0, json.dumps({'state': 'deferred', 'reason': 'owner_busy'}), '')
        with mock.patch.object(wiki_selected_laya, 'selected', return_value=bound), \
             mock.patch.object(wiki_selected_laya, 'recheck'), \
             mock.patch('memvane.wiki.compile.wiki_compile_worker._safe_queue_path', return_value=project / 'queue'), \
             mock.patch('memvane.wiki.workspace.lifecycle_requests.claim', return_value=None), \
             mock.patch.object(wiki_selected_laya.subprocess, 'run', return_value=child):
            result = wiki_selected_laya_selected_execution.run(project)
        self.assertEqual(result, {'state': 'deferred', 'reason': 'owner_busy'})
        from memvane.app import runtime_dispatch as laya_runtime_dispatch
        self.assertEqual(laya_runtime_dispatch._deferred_detail(result),
                         'deferred:deferred:owner_busy')

    def test_invalid_worker_deferral_cannot_claim_drained(self):
        project = Path(os.environ['HOME']).resolve()
        bound = ({'projectRoot': str(project)}, {})
        for reason in ('untrusted', ['owner_busy'], None):
            child = subprocess.CompletedProcess([], 0, json.dumps({
                'state': 'deferred', 'reason': reason, 'processed': 0}), '')
            with self.subTest(reason=reason), \
                 mock.patch.object(wiki_selected_laya, 'selected', return_value=bound), \
                 mock.patch.object(wiki_selected_laya, 'recheck'), \
                 mock.patch('memvane.wiki.compile.wiki_compile_worker._safe_queue_path', return_value=project / 'queue'), \
                 mock.patch('memvane.wiki.workspace.lifecycle_requests.claim', return_value=None), \
                 mock.patch.object(wiki_selected_laya.subprocess, 'run', return_value=child):
                self.assertEqual(wiki_selected_laya_selected_execution.run(project),
                                 {'state': 'deferred', 'reason': 'compile_worker_result_invalid'})

    def test_post_lock_worker_proof_deferral_is_not_swallowed(self):
        output = io.StringIO()
        root = Path(os.environ['HOME']).resolve()
        found = {'projectRoot': str(root), 'config': {'indexing': True, 'compile': {}}}
        fd = os.open(root / 'run-lock', os.O_CREAT | os.O_RDWR, 0o600)
        with mock.patch.object(sys, 'argv', ['wiki_compile_worker.py', '--cwd', str(root), '--json']), \
             mock.patch.object(wiki_compile_worker.qmd_config, 'find_project_config', return_value=found), \
             mock.patch.object(wiki_compile_worker.qmd_config, 'compile_active', return_value=True), \
             mock.patch.object(wiki_compile_worker, 'try_run_lock', return_value=fd), \
             mock.patch.object(wiki_compile_worker.cp, 'validate_compile_run_lock', return_value=root), \
             mock.patch.object(wiki_selected_laya, 'selected',
                               side_effect=[({'projectRoot': str(root)}, {}), ResourceDeferred('owner_busy')]), \
             redirect_stdout(output):
            self.assertEqual(wiki_compile_worker.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {'state': 'deferred', 'reason': 'owner_busy'})

    def test_claimed_job_is_requeued_if_owner_becomes_busy(self):
        from memvane.wiki.intake import wiki_backfill as laya_wiki_backfill
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as temp:
            root = Path(temp).resolve()
            queue = root / 'queue.jsonl'
            raw = json.dumps({'sourcePath': 'synthetic.md'})
            queue.write_text(raw + '\n')
            args = SimpleNamespace(json=True, flush_all=True)
            with mock.patch.object(wiki_compile_worker, 'recover_pending_refreshes'), \
                 mock.patch.object(laya_wiki_backfill, 'reconcile_discards'), \
                 mock.patch.object(wiki_compile_worker, 'dedup_jobs',
                                   side_effect=lambda rows: (rows, [])), \
                 mock.patch.object(wiki_compile_worker, 'batch_ready', return_value=True), \
                 mock.patch.object(wiki_compile_worker, 'process_job',
                                   side_effect=ResourceDeferred('owner_busy')):
                with self.assertRaisesRegex(ResourceDeferred, '^owner_busy$'):
                    wiki_compile_worker._main_locked(args, root, {}, {}, queue, None)
            self.assertEqual(queue.read_text().splitlines(), [raw])
            self.assertFalse(list(root.glob('queue.jsonl.claimed.*')))

    def test_compile_owner_busy_does_not_consume_attempt(self):
        failure = policy.DispatchFailure('target_deferred',
                                         detail='deferred:deferred:owner_busy')
        step = policy.settle(policy.RETRY_CAP_ATTEMPTS - 1, done=False, now=100,
                             error=failure, kind='laya-compile')
        self.assertFalse(step['ack'])
        self.assertEqual(step['count'], 0)
        self.assertNotIn('drop', step)

    def test_other_or_untrusted_deferred_results_remain_counted(self):
        for kind, detail in (('laya-sync', 'deferred:deferred:owner_busy'),
                             ('laya-compile', 'deferred:pending:unlabeled'),
                             ('laya-compile', 'deferred:deferred:owner_busy:extra'),
                             ('laya-compile', 'deferred:deferred:owner_busy\nprivate')):
            with self.subTest(kind=kind, detail=detail):
                failure = policy.DispatchFailure('target_deferred', detail=detail)
                step = policy.settle(0, done=False, now=100, error=failure, kind=kind)
                self.assertEqual(step['count'], 1)
                self.assertFalse(step['ack'])

    def test_worker_stderr_details_are_fixed_and_do_not_leak_paths(self):
        project = Path(os.environ['HOME']).resolve()
        bound = ({'projectRoot': str(project)}, {})
        for stderr in ('Traceback\nValueError: /private/sensitive\n',
                       'Traceback\nRuntimeError: secret-token\n'):
            child = subprocess.CompletedProcess([], 1, '', stderr)
            with self.subTest(stderr=stderr), \
                 mock.patch.object(wiki_selected_laya, 'selected', return_value=bound), \
                 mock.patch.object(wiki_selected_laya, 'recheck'), \
                 mock.patch('memvane.wiki.compile.wiki_compile_worker._safe_queue_path', return_value=project / 'queue'), \
                 mock.patch('memvane.wiki.workspace.lifecycle_requests.claim', return_value=None), \
                 mock.patch.object(wiki_selected_laya.subprocess, 'run', return_value=child):
                result = wiki_selected_laya_selected_execution.run(project)
            self.assertEqual(result['reason'], 'compile_worker_failed')
            self.assertIn(result.get('detail'), {'worker_value_error', 'worker_runtime_error'})
            self.assertNotIn('private', json.dumps(result))
            self.assertNotIn('secret', json.dumps(result))

    def test_unknown_deferral_reason_does_not_leak_message(self):
        with mock.patch.object(wiki_selected_laya_selected_execution, '_run',
                               side_effect=ResourceDeferred('/private/secret')):
            self.assertEqual(wiki_selected_laya_selected_execution.run(Path(os.environ['HOME'])),
                             {'state': 'deferred', 'reason': 'resource_deferred'})


if __name__ == '__main__':
    print('python', sys.version.split()[0], 'executable', sys.executable, flush=True)
    unittest.main()
