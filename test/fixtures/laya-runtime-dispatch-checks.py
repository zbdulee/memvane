"""Synthetic UID wake routing checks; no daemon, model, or real user queue."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import laya_runtime_dispatch as dispatch
import laya_wake_entry as entry
from runtime_lease import coordination_lock
from runtime_wake_policy import DispatchFailure, failure_reason
from laya_product_bundle import (PLUGIN_PREFIXES, PLUGIN_ROOT_FILES,
                                 prepare_full_plugin_candidate)


class DispatchBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        self.bundles = []
        for name in ('old', 'new'):
            root = self.base / name
            (root / 'core').mkdir(parents=True)
            for filename in ('laya_product_sync_worker.py', 'wiki_selected_laya.py'):
                (root / 'core' / filename).write_text('pass\n')
            self.bundles.append(root)

    def proof(self, root):
        return {'pluginRoot': str(root), 'sourceCommit': 'a' * 40 if root == self.bundles[0] else 'b' * 40,
                'bundleManifestSha256': 'c' * 64, 'ownershipManifestSha256': 'd' * 64,
                'productRouteAllowed': True, 'installId': '1' * 32,
                'scopeId': '2' * 32, 'dbIncarnation': '3' * 32,
                'journalPath': str(self.base / 'journal'), 'selectionEpoch': 1,
                'generation': 'g-' + '4' * 32}

    def route(self, state='candidate'):
        return {'state': state, 'projectRoot': str(self.project),
                'dbPath': str(self.base / 'index.sqlite'),
                'queuePath': str(self.base / 'claims.sqlite')}

    def test_old_to_new_new_to_old_and_same_bundle_use_fixed_child_argv(self):
        for owner, target, kind, state in ((0, 1, 'laya-sync', 'indexed'),
                                           (1, 0, 'laya-sync', 'unchanged'),
                                           (1, 1, 'laya-compile', 'drained')):
            with self.subTest(owner=owner, target=target, kind=kind):
                calls = []
                def child(argv, project, seconds):
                    calls.append((argv, project, seconds))
                    return types.SimpleNamespace(returncode=0,
                                                 stdout=json.dumps({'state': state}).encode())
                with patch('laya_index_host_route.select_route', return_value=self.route()), \
                     patch('laya_install_journal.verify_dispatch_target',
                           return_value=self.proof(self.bundles[target])), \
                     patch.object(dispatch, '_run_child', side_effect=child):
                    self.assertEqual(dispatch.run(kind, self.project)['state'], state)
                argv, project, seconds = calls[0]
                self.assertEqual(argv[:4], [sys.executable, '-B', str(CORE / 'laya_wake_entry.py'), kind])
                self.assertEqual(argv[4:7], [str(self.project), str(self.bundles[target]),
                                             self.proof(self.bundles[target])['sourceCommit']])
                self.assertEqual(project, str(self.project))
                self.assertGreater(seconds, 600 if kind == 'laya-sync' else 120)
                self.assertEqual(dispatch._child_env()['QMD_RUNTIME_DISPATCH'], '1')
                self.assertNotIn('QMD_PLUGIN_ROOT', dispatch._child_env())

    def test_initial_build_chooses_pending_sealed_bundle(self):
        root = self.bundles[1]
        proof = self.proof(root)
        identity = {'journalPath': str(self.base / 'journal'), 'routeSha256': 'e' * 64,
                    'attestationSha256': 'f' * 64, 'installId': '1' * 32,
                    'scopeId': '2' * 32, 'dbIncarnation': '3' * 32,
                    'candidateDb': str(self.base / 'index.sqlite'),
                    'queueDb': str(self.base / 'claims.sqlite')}
        record = {'identity': identity, 'phase': 'pending'}
        journal = {'state': 'candidate_ready', 'pending': proof,
                   'projectRoot': str(self.project), 'epoch': 1}
        approval = {'pendingRouteSha256': identity['routeSha256'],
                    'attestationSha256': identity['attestationSha256']}
        with patch('laya_index_host_route.select_route', return_value=self.route('legacy')), \
             patch('laya_initial_build.lookup', return_value=record), \
             patch('laya_install_journal.inspect_journal', return_value=journal), \
             patch('laya_install_journal.preflight_product_candidate', return_value=approval):
            self.assertEqual(dispatch.resolve('laya-sync', self.project)['root'], str(root))

    def test_tamper_and_foreign_identity_fail_closed(self):
        root = self.bundles[0]
        worker = root / 'core' / 'laya_product_sync_worker.py'
        worker.chmod(0o666)
        with patch('laya_index_host_route.select_route', return_value=self.route()), \
             patch('laya_install_journal.verify_dispatch_target', return_value=self.proof(root)):
            with self.assertRaises(DispatchFailure) as caught:
                dispatch.resolve('laya-sync', self.project)
            self.assertEqual(failure_reason(caught.exception),
                             'dispatch:target_entry_invalid:' + 'a' * 40 + ':' + 'c' * 64)
        worker.chmod(0o644)
        with patch('laya_index_host_route.select_route', return_value=self.route()), \
             patch('laya_install_journal.verify_dispatch_target', side_effect=ValueError('private path')):
            with self.assertRaises(DispatchFailure) as caught:
                dispatch.resolve('laya-sync', self.project)
            self.assertEqual(failure_reason(caught.exception), 'dispatch:target_journal_changed:none:none')
        with self.assertRaises(DispatchFailure):
            dispatch.resolve('arbitrary-command', self.project)

    def test_child_rechecks_digest_before_worker_import(self):
        root = self.bundles[0]
        fake = types.ModuleType('laya_product_bundle')
        fake.inspect_full_plugin_candidate = lambda _: self.proof(root)
        worker = types.ModuleType('laya_product_sync_worker')
        invoked = []
        worker.run = lambda _: invoked.append(True)
        with patch.dict(sys.modules, {'laya_product_bundle': fake,
                                      'laya_product_sync_worker': worker}), \
             patch.object(sys, 'path', sys.path.copy()):
            args = ['laya-sync', str(self.project), str(root), 'a' * 40,
                    '0' * 64, 'd' * 64]
            self.assertEqual(entry.main(args), 3)
        self.assertEqual(invoked, [])

    def test_deferred_result_cannot_ack(self):
        root = self.bundles[0]
        with patch('laya_index_host_route.select_route', return_value=self.route()), \
             patch('laya_install_journal.verify_dispatch_target', return_value=self.proof(root)), \
             patch.object(dispatch, '_run_child', return_value=types.SimpleNamespace(
                 returncode=0, stdout=b'{"state":"deferred"}')):
            with self.assertRaises(DispatchFailure) as caught:
                dispatch.run('laya-sync', self.project)
            self.assertEqual(failure_reason(caught.exception),
                             'dispatch:target_deferred:' + 'a' * 40 + ':' + 'c' * 64)

    def test_preparing_owner_result_is_an_uncounted_wait(self):
        root = self.bundles[0]
        for stdout, code in ((b'{"reason":"laya_service_preparing","state":"deferred"}',
                              'target_preparing'),
                             (b'{"reason":"laya_service_backoff","state":"deferred"}',
                              'target_deferred')):
            with self.subTest(code=code), \
                 patch('laya_index_host_route.select_route', return_value=self.route()), \
                 patch('laya_install_journal.verify_dispatch_target',
                       return_value=self.proof(root)), \
                 patch.object(dispatch, '_run_child', return_value=types.SimpleNamespace(
                     returncode=0, stdout=stdout)):
                with self.assertRaises(DispatchFailure) as caught:
                    dispatch.run('laya-sync', self.project)
                self.assertEqual(caught.exception.code, code)
                if code == 'target_deferred':
                    # The child's fixed reason code survives for the failure record.
                    self.assertEqual(caught.exception.detail,
                                     'deferred:deferred:laya_service_backoff')
        with patch('laya_index_host_route.select_route', return_value=self.route()), \
             patch('laya_install_journal.verify_dispatch_target', return_value=self.proof(root)), \
             patch.object(dispatch, '_run_child', return_value=types.SimpleNamespace(
                 returncode=0, stdout=json.dumps({'state': 'deferred',
                     'reason': 'laya_sync_yield_priority', 'detail': 'Private Path/x.md'}).encode())):
            with self.assertRaises(DispatchFailure) as caught:
                dispatch.run('laya-sync', self.project)
        self.assertEqual(caught.exception.detail,
                         'deferred:deferred:laya_sync_yield_priority:_rivate__ath_x.md')
        with patch('laya_index_host_route.select_route', return_value=self.route()), \
             patch('laya_install_journal.verify_dispatch_target', return_value=self.proof(root)), \
             patch.object(dispatch, '_run_child', return_value=types.SimpleNamespace(
                 returncode=0, stdout=b'{"state":"pending"}')):
            with self.assertRaises(DispatchFailure) as caught:
                dispatch.run('laya-sync', self.project)
        self.assertEqual(caught.exception.detail, 'deferred:pending:unlabeled')

    def test_failed_child_keeps_a_bounded_stderr_tail(self):
        root = self.bundles[0]
        with patch('laya_index_host_route.select_route', return_value=self.route()), \
             patch('laya_install_journal.verify_dispatch_target', return_value=self.proof(root)), \
             patch.object(dispatch, '_run_child', return_value=types.SimpleNamespace(
                 returncode=1, stdout=b'',
                 stderr=b'Traceback\nIndexContractError: provider_unavailable\n')):
            with self.assertRaises(DispatchFailure) as caught:
                dispatch.run('laya-sync', self.project)
        self.assertEqual(caught.exception.code, 'target_child_failed')
        self.assertIn('provider_unavailable', caught.exception.detail)
        self.assertTrue(caught.exception.detail.startswith('exit 1'))
        # The ledger reason keeps its fixed non-sensitive shape.
        self.assertEqual(failure_reason(caught.exception),
                         'dispatch:target_child_failed:' + 'a' * 40 + ':' + 'c' * 64)
        failed = dispatch._run_child([sys.executable, '-c',
            'import sys;sys.stderr.write("y"*9000+"LAST");sys.exit(3)'],
            str(self.project), 10)
        self.assertEqual(failed.returncode, 3)
        self.assertEqual(len(failed.stderr), dispatch.STDERR_TAIL)
        self.assertTrue(failed.stderr.endswith(b'LAST'))
        # A large stderr burst never blocks the child on a full pipe.
        flood = dispatch._run_child([sys.executable, '-c',
            'import sys;sys.stderr.write("z"*300000);print("{}")'], str(self.project), 10)
        self.assertEqual((flood.returncode, flood.stdout.strip()), (0, b'{}'))
        # A descendant that inherits stderr past the child never holds the
        # wake open: completion still follows stdout EOF and child exit. It
        # reads the dispatcher's stdin pipe, which closes on return.
        held = ('import subprocess,sys;'
                'subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],'
                'stdout=subprocess.DEVNULL);print("{}")')
        done = dispatch._run_child([sys.executable, '-c', held], str(self.project), 10)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, b'{}'))

    def test_same_bundle_new_install_or_db_cannot_ack_old_work(self):
        root = self.bundles[0]
        start = {**self.proof(root), 'terminal': False, 'project': str(self.project),
                 'root': str(root), 'dbPath': str(self.base / 'index.sqlite'),
                 'queuePath': str(self.base / 'claims.sqlite')}
        completed = types.SimpleNamespace(returncode=0, stdout=b'{"state":"indexed"}')
        for changed in ({'installId': '9' * 32},
                        {'dbIncarnation': '8' * 32},
                        {'dbPath': str(self.base / 'replacement.sqlite')}):
            with self.subTest(changed=changed), \
                 patch.object(dispatch, 'resolve', side_effect=[start, {**start, **changed}]), \
                 patch.object(dispatch, '_run_child', return_value=completed):
                with self.assertRaises(DispatchFailure) as caught:
                    dispatch.run('laya-sync', self.project)
                self.assertEqual(caught.exception.code, 'target_journal_changed')

    def test_same_install_sync_epoch_and_generation_progress_can_ack(self):
        root = self.bundles[0]
        start = {**self.proof(root), 'terminal': False, 'project': str(self.project),
                 'root': str(root), 'dbPath': str(self.base / 'index.sqlite'),
                 'queuePath': str(self.base / 'claims.sqlite')}
        advanced = {**start, 'selectionEpoch': 2, 'generation': 'g-' + '5' * 32}
        with patch.object(dispatch, 'resolve', side_effect=[start, advanced]), \
             patch.object(dispatch, '_run_child', return_value=types.SimpleNamespace(
                 returncode=0, stdout=b'{"state":"indexed"}')):
            self.assertEqual(dispatch.run('laya-sync', self.project), {'state': 'indexed'})

    def test_qa_barrier_adds_time_only_when_explicitly_armed(self):
        root = self.bundles[0]
        target = {**self.proof(root), 'root': str(root), 'project': str(self.project),
                  'terminal': False, 'dbPath': str(self.base / 'index.sqlite'),
                  'queuePath': str(self.base / 'claims.sqlite')}
        seconds = []
        def child(_argv, _project, budget):
            seconds.append(budget)
            return types.SimpleNamespace(returncode=0, stdout=b'{"state":"unchanged"}')
        with patch.object(dispatch, 'resolve', return_value=target), \
             patch.object(dispatch, '_run_child', side_effect=child):
            with patch.dict(os.environ, {'LAYA_QA_SYNC_BARRIER': ''}):
                dispatch.run('laya-sync', self.project)
            with patch.dict(os.environ, {'LAYA_QA_SYNC_BARRIER': str(self.base / 'arm')}):
                dispatch.run('laya-sync', self.project)
        self.assertEqual(seconds, [dispatch.SYNC_SECONDS,
                                   dispatch.SYNC_SECONDS + dispatch.QA_BARRIER_SECONDS])

    def test_output_cap_and_owned_nested_child_timeout(self):
        owner = coordination_lock(self.base / 'child-owner.lock')
        try:
            checked = dispatch._run_child([sys.executable, '-c',
                'import os,sys;os.fstat(int(sys.argv[1]));print("held")', str(owner)],
                str(self.project), 3, owner_fd=owner)
            self.assertEqual(checked.stdout.strip(), b'held')
        finally:
            os.close(owner)
        with self.assertRaises(ValueError):
            dispatch._run_child([sys.executable, '-c',
                                 'import sys;sys.stdout.write("x"*70000)'],
                                str(self.project), 3)
        marker = self.base / 'nested-finished'
        nested = ('import time,pathlib;time.sleep(1);'
                  'pathlib.Path(%r).write_text("orphan")' % str(marker))
        program = ('import subprocess,sys,time;'
                   'subprocess.Popen([sys.executable,"-c",%r]);'
                   'time.sleep(2)' % nested)
        with self.assertRaises(subprocess.TimeoutExpired):
            dispatch._run_child([sys.executable, '-c', program, str(marker)],
                                str(self.project), .2)
        time.sleep(1.1)
        self.assertFalse(marker.exists(), 'a nested child outlived its owned process group')


class SealedChildSubprocess(unittest.TestCase):
    """Two disposable exact-commit bundles, executed by the real bootstrap."""

    def test_old_new_child_paths_and_preimport_tamper_refusal(self):
        with tempfile.TemporaryDirectory(prefix='laya-sealed-child-') as name:
            base = Path(name).resolve()
            source, project = base / 'source', base / 'project'
            source.mkdir()
            project.mkdir()
            repo = CORE.parent
            names = subprocess.run(['git', 'ls-files', '--cached', '--others',
                                    '--exclude-standard', '--',
                                    *[p[:-1] for p in PLUGIN_PREFIXES],
                                    *sorted(PLUGIN_ROOT_FILES)], cwd=repo, check=True,
                                   capture_output=True, text=True).stdout.splitlines()
            for name in names:
                original = repo / name
                if original.is_file() and not original.is_symlink():
                    destination = source / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, destination)
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            def commit(label):
                stub = (source / 'core' / 'laya_product_sync_worker.py')
                stub.write_text('import os,time\nfrom pathlib import Path\n'
                                'marker=os.environ.get("SEALED_STUB_MARKER")\n'
                                'if marker: Path(marker).write_text("imported")\n'
                                'def run(_project):\n'
                                ' hold=os.environ.get("SEALED_STUB_HOLD")\n'
                                ' if hold: time.sleep(1);Path(hold).write_text("late")\n'
                                ' return {"state":"indexed","bundle":%r}\n' % label)
                subprocess.run(['git', '-C', str(source), 'add', '-A'], check=True)
                subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture',
                                '-c', 'user.email=fixture@example.invalid',
                                'commit', '-qm', label], check=True)
                sha = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                     check=True, capture_output=True, text=True).stdout.strip()
                return prepare_full_plugin_candidate(source, base / ('plugin-' + label),
                                                     commit_id=sha)
            old, new = commit('old'), commit('new')
            for proof, label in ((new, 'new'), (old, 'old'), (new, 'new')):
                marker = base / ('marker-' + label)
                marker.unlink(missing_ok=True)
                args = [sys.executable, '-B', str(CORE / 'laya_wake_entry.py'),
                        'laya-sync', str(project), proof['pluginRoot'],
                        proof['sourceCommit'], proof['bundleManifestSha256'],
                        proof['ownershipManifestSha256']]
                env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                       'SEALED_STUB_MARKER': str(marker)}
                executed = subprocess.run(args, cwd=project, env=env, capture_output=True,
                                          text=True, timeout=20)
                self.assertEqual(executed.returncode, 0, executed.stderr)
                self.assertEqual(json.loads(executed.stdout)['bundle'], label)
                self.assertEqual(marker.read_text(), 'imported')
            hold = base / 'orphaned-work-finished'
            watched_env = {**env, 'QMD_LAYA_WAKE_OWNER_PIPE': '1',
                           'SEALED_STUB_HOLD': str(hold)}
            watched = subprocess.Popen(args, cwd=project, env=watched_env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            try:
                time.sleep(.2)
                watched.stdin.close()  # model a UID worker dying mid-task
                self.assertNotEqual(watched.wait(timeout=3), 0)
                time.sleep(1.1)
                self.assertFalse(hold.exists(), 'owner death left target work running')
            finally:
                if watched.poll() is None:
                    watched.kill()
                    watched.wait(timeout=2)
                watched.stdout.close()
                watched.stderr.close()
            worker = Path(old['pluginRoot']) / 'core' / 'laya_product_sync_worker.py'
            worker.chmod(0o600)
            worker.write_text(worker.read_text() + '\n# altered after seal\n')
            marker = base / 'tampered-marker'
            env['SEALED_STUB_MARKER'] = str(marker)
            refused = subprocess.run([sys.executable, '-B', str(CORE / 'laya_wake_entry.py'),
                                      'laya-sync', str(project), old['pluginRoot'],
                                      old['sourceCommit'], old['bundleManifestSha256'],
                                      old['ownershipManifestSha256']],
                                     cwd=project, env=env, capture_output=True,
                                     text=True, timeout=20)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(marker.exists(), 'target code imported before sealed-byte proof')


if __name__ == '__main__':
    unittest.main()
