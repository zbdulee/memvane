"""One UID-shared Laya wake handed to the executing plugin's ``entry/run.py wake`` child.

The dispatcher accepts only the ``laya-sync`` and ``laya-compile`` kinds,
proves the target through ``journal.api.dispatch_target`` and runs the fixed
wake command for that project with a sanitized environment that keeps the test
runtime root. Only ``indexed``/``unchanged`` (sync) or ``drained`` (compile)
ACK; any other result, a failed child or a target whose install, scope or
store changed meanwhile keeps the wake. Failures carry fixed codes; a failed
child's stderr survives only as a bounded tail.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

from memvane.app import runtime_dispatch as dispatch
from memvane.journal.types import DispatchTargetError
from memvane.kernel.bundle_paths import entry_argv
from memvane.runtime import api as runtime
from memvane.runtime.types import DispatchFailure
from tests.support.case import FixtureCase

ROOT = Path(__file__).resolve().parents[4]


def completed(stdout=b'{"state":"indexed"}', returncode=0, stderr=b''):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class DispatchCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        self.project.mkdir()
        self.target = {'terminal': False, 'installId': '1' * 32, 'scopeId': '2' * 32,
                       'storeIncarnation': '3' * 32, 'storePath': str(self.home / 'store.sqlite'),
                       'journalPath': str(self.home / 'journal.sqlite'), 'selectionEpoch': 1,
                       'generation': 'g-' + '4' * 32}
        self.lookups = []

    def dispatch_target(self, *targets):
        """``journal.api.dispatch_target`` answering ``targets`` in turn (the last repeats)."""
        answers = list(targets) or [self.target]

        def lookup(project, *, build):
            self.lookups.append((str(project), build))
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, BaseException):
                raise answer
            return dict(answer)
        return mock.patch('memvane.journal.api.dispatch_target', side_effect=lookup)

    def child(self, *results):
        """``_run_child`` answering ``results`` in turn; calls land in ``self.children``."""
        self.children = []
        answers = list(results) or [completed()]

        def run(argv, project, seconds, **options):
            self.children.append((argv, project, seconds, options))
            return answers.pop(0) if len(answers) > 1 else answers[0]
        return mock.patch.object(dispatch, '_run_child', side_effect=run)

    def failure(self, kind='laya-sync'):
        with self.assertRaises(DispatchFailure) as caught:
            dispatch.run(kind, self.project)
        return caught.exception


class Kinds(DispatchCase):
    def test_unknown_wake_kinds_are_refused_before_any_lookup_or_child(self):
        for kind in ('arbitrary-command', 'learning', 'auto', 'index', '', 'laya-sync ', None):
            with self.subTest(kind=kind), self.dispatch_target(), self.child():
                with self.assertRaises(DispatchFailure) as caught:
                    dispatch.resolve(kind, self.project)
                self.assertEqual(caught.exception.code, 'kind_invalid')
                self.assertEqual(self.failure(kind).code, 'kind_invalid')
                self.assertEqual((self.lookups, self.children), ([], []))

    def test_resolve_proves_the_target_through_the_journal(self):
        with self.dispatch_target({'terminal': True}):
            self.assertEqual(dispatch.resolve('laya-sync', self.project),
                             {'terminal': True, 'project': str(self.project)})
        with self.dispatch_target():
            self.assertEqual(dispatch.resolve('laya-compile', self.project),
                             {**self.target, 'project': str(self.project)})
        # Only the sync kind admits a registered, not yet selected build.
        self.assertEqual(self.lookups, [(str(self.project), True), (str(self.project), False)])
        for code in ('target_unavailable', 'state_format_mismatch', 'writer_floor', 'target_entry_invalid'):
            with self.subTest(code=code), self.dispatch_target(DispatchTargetError(code)):
                with self.assertRaises(DispatchFailure) as caught:
                    dispatch.resolve('laya-sync', self.project)
                self.assertEqual(caught.exception.code, code)


class Child(DispatchCase):
    def test_the_child_is_the_fixed_wake_command_of_the_project(self):
        for kind, state, seconds in (('laya-sync', 'indexed', dispatch.SYNC_SECONDS),
                                     ('laya-sync', 'unchanged', dispatch.SYNC_SECONDS),
                                     ('laya-compile', 'drained', dispatch.COMPILE_SECONDS)):
            with self.subTest(kind=kind, state=state), self.dispatch_target(), \
                    self.child(completed(json.dumps({'state': state}).encode())):
                self.assertEqual(dispatch.run(kind, self.project), {'state': state})
                (argv, cwd, budget, options), = self.children
                self.assertEqual(argv, entry_argv('wake', kind, str(self.project)))
                self.assertEqual((cwd, budget, options), (str(self.project), seconds, {}))
        with self.dispatch_target(), self.child():
            dispatch.run('laya-sync', self.project, owner_fd=7)
        self.assertEqual(self.children[0][3], {'owner_fd': 7})

    def test_the_child_environment_keeps_only_the_runtime_and_guard_envelope(self):
        extra = {'MEMVANE_PRIVATE_SECRET': 'x', 'PYTHONPATH': '/elsewhere', 'QMD_SANDBOX': '1'}
        with mock.patch.dict(os.environ, extra):
            env = dispatch._child_env()
        self.assertEqual((env['QMD_RUNTIME_DISPATCH'], env['PYTHONDONTWRITEBYTECODE']), ('1', '1'))
        self.assertEqual(env['MEMVANE_TEST_RUNTIME_ROOT'], str(self.runtime))
        self.assertEqual([key for key in extra if key in env], [])

    def test_a_terminal_target_is_unchanged_without_a_child(self):
        with self.dispatch_target({'terminal': True}), self.child():
            self.assertEqual(dispatch.run('laya-sync', self.project), {'state': 'unchanged'})
        self.assertEqual(self.children, [])

    def test_a_real_dispatch_child_resolves_the_same_test_runtime_root(self):
        # The child's environment is sanitized; it must keep the test runtime
        # root, or its coordinator and wake queue would resolve the user's.
        code = ('import json;from memvane.runtime import api;'
                'print(json.dumps([str(api.service_root()), str(api.service_root())]))')
        parent = runtime.service_root(create=True)
        child = dispatch._run_child([sys.executable, '-B', '-c', code], str(ROOT), 60)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout.decode()), [str(parent), str(parent)])
        self.assertEqual(parent, self.runtime / 'laya-inference')


class Results(DispatchCase):
    def test_results_other_than_done_cannot_ack(self):
        cases = ((b'{"state":"deferred"}', 'laya-sync', 'deferred:deferred:unlabeled'),
                 (b'{"state":"pending"}', 'laya-sync', 'deferred:pending:unlabeled'),
                 (b'{"state":"pending","progressKind":"vector"}', 'laya-sync',
                  'deferred:pending:schema3_progress_vector'),
                 (b'{"state":"blocked","reason":"owner_busy"}', 'laya-sync', 'deferred:blocked:owner_busy'),
                 (b'{"state":"indexed"}', 'laya-compile', 'deferred:indexed:unlabeled'),
                 (b'{"state":"drained"}', 'laya-sync', 'deferred:drained:unlabeled'),
                 (b'{"state":"done"}', 'laya-sync', None))
        for stdout, kind, detail in cases:
            with self.subTest(stdout=stdout, kind=kind), self.dispatch_target(), self.child(completed(stdout)):
                failure = self.failure(kind)
                if detail is None:
                    self.assertEqual(failure.code, 'target_result_invalid')
                else:
                    self.assertEqual((failure.code, failure.detail), ('target_deferred', detail))
        for stdout in (b'not json', b'[]', b'{"state":7}'):
            with self.subTest(stdout=stdout), self.dispatch_target(), self.child(completed(stdout)):
                self.assertEqual(self.failure().code, 'target_result_invalid')

    def test_a_deferral_detail_is_fixed_codes_only(self):
        stdout = json.dumps({'state': 'deferred', 'reason': 'laya_sync_yield_priority',
                             'detail': 'Private Path/x.md'}).encode()
        with self.dispatch_target(), self.child(completed(stdout)):
            self.assertEqual(self.failure().detail, 'deferred:deferred:laya_sync_yield_priority:_rivate__ath_x.md')

    def test_a_preparing_owner_is_an_uncounted_wait(self):
        for reason, code in (('laya_service_preparing', 'target_preparing'),
                             ('laya_service_backoff', 'target_deferred')):
            stdout = json.dumps({'state': 'deferred', 'reason': reason}).encode()
            with self.subTest(reason=reason), self.dispatch_target(), self.child(completed(stdout)):
                self.assertEqual(self.failure().code, code)
        # Only the sync kind waits on its embedding owner.
        stdout = b'{"state":"deferred","reason":"laya_service_preparing"}'
        with self.dispatch_target(), self.child(completed(stdout)):
            self.assertEqual(self.failure('laya-compile').code, 'target_deferred')

    def test_a_deferred_fill_names_its_member_in_the_install_log(self):
        card = 'service-engineering-wiki/entities/리드라우팅-카드.md'
        for path, expected in ((card, card), ('/abs/x.md', None), ('../x.md', None), ('notes.txt', None)):
            events = []
            stdout = json.dumps({'state': 'deferred', 'reason': 'provider_result_invalid', 'path': path,
                                 'cause': 'chunk_pos_not_increasing'}, ensure_ascii=False).encode()
            with self.subTest(path=path), self.dispatch_target(), self.child(completed(stdout)), \
                    mock.patch('memvane.diagnostics.api.state_root_of', return_value=self.home), \
                    mock.patch('memvane.diagnostics.api.emit',
                               side_effect=lambda name, **fields: events.append((name, fields))):
                self.assertEqual(self.failure().detail, 'deferred:deferred:provider_result_invalid')
                (name, fields), = events
                self.assertEqual((name, fields['outcome'], fields['reason'], fields.get('path')),
                                 ('wake.sync', 'failed', 'target_deferred', expected))
                self.assertEqual(fields.get('cause'), 'chunk_pos_not_increasing' if expected else None)

    def test_a_published_sync_logs_the_wake_and_the_publish(self):
        events = []
        stdout = b'{"state":"indexed","generation":"g-' + b'5' * 32 + b'"}'
        with self.dispatch_target(), self.child(completed(stdout)), \
                mock.patch('memvane.diagnostics.api.state_root_of', return_value=self.home), \
                mock.patch('memvane.diagnostics.api.emit',
                           side_effect=lambda name, **fields: events.append((name, fields))):
            dispatch.run('laya-sync', self.project)
        self.assertEqual([(name, fields['outcome']) for name, fields in events],
                         [('wake.sync', 'indexed'), ('index.publish', 'published')])
        self.assertEqual((events[1][1]['generation'], events[1][1]['previousGeneration']),
                         ('g-' + '5' * 32, self.target['generation']))

    def test_a_failed_child_keeps_only_a_bounded_stderr_tail(self):
        with self.dispatch_target(), self.child(completed(b'', 1, b'Traceback\nIndexContractError: provider_unavailable\n')):
            failure = self.failure()
        self.assertEqual(failure.code, 'target_child_failed')
        self.assertTrue(failure.detail.startswith('exit 1\n'))
        self.assertIn('provider_unavailable', failure.detail)
        self.assertEqual(str(failure), 'target_child_failed')
        failed = dispatch._run_child([sys.executable, '-c',
                                      'import sys;sys.stderr.write("y"*9000+"LAST");sys.exit(3)'],
                                     str(self.project), 30)
        self.assertEqual((failed.returncode, len(failed.stderr)), (3, dispatch.STDERR_TAIL))
        self.assertTrue(failed.stderr.endswith(b'LAST'))
        # A large stderr burst never blocks the child on a full pipe.
        flood = dispatch._run_child([sys.executable, '-c', 'import sys;sys.stderr.write("z"*300000);print("{}")'],
                                    str(self.project), 30)
        self.assertEqual((flood.returncode, flood.stdout.strip()), (0, b'{}'))
        # A descendant holding stderr past the child never holds the wake open.
        held = ('import subprocess,sys;subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],'
                'stdout=subprocess.DEVNULL);print("{}")')
        done = dispatch._run_child([sys.executable, '-c', held], str(self.project), 30)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, b'{}'))

    def test_another_install_scope_or_store_cannot_ack_old_work(self):
        for field in dispatch.STABLE_TARGET_FIELDS:
            if field == 'project':
                continue
            changed = {**self.target, field: '9' * 32 if field != 'storePath' else str(self.home / 'other.sqlite')}
            with self.subTest(field=field), self.dispatch_target(self.target, changed), self.child():
                self.assertEqual(self.failure().code, 'target_journal_changed')
        with self.dispatch_target(self.target, {'terminal': True}), self.child():
            self.assertEqual(self.failure().code, 'target_journal_changed')

    def test_the_same_install_may_advance_its_epoch_and_generation(self):
        advanced = {**self.target, 'selectionEpoch': 2, 'generation': 'g-' + '5' * 32}
        with self.dispatch_target(self.target, advanced), self.child():
            self.assertEqual(dispatch.run('laya-sync', self.project), {'state': 'indexed'})

    def test_the_qa_barrier_adds_time_only_when_armed(self):
        budgets = []
        for armed in ('', str(self.home / 'arm')):
            with mock.patch.dict(os.environ, {'LAYA_QA_SYNC_BARRIER': armed}), self.dispatch_target(), \
                    self.child(completed(b'{"state":"unchanged"}')):
                dispatch.run('laya-sync', self.project)
                budgets.append(self.children[0][2])
        self.assertEqual(budgets, [dispatch.SYNC_SECONDS, dispatch.SYNC_SECONDS + dispatch.QA_BARRIER_SECONDS])


class OwnedChild(DispatchCase):
    def test_the_owner_fd_reaches_the_child_and_output_is_capped(self):
        owner = runtime.coordination_lock(self.home / 'child-owner.lock')
        try:
            checked = dispatch._run_child([sys.executable, '-c',
                                           'import os,sys;os.fstat(int(sys.argv[1]));print("held")', str(owner)],
                                          str(self.project), 30, owner_fd=owner)
            self.assertEqual(checked.stdout.strip(), b'held')
        finally:
            os.close(owner)
        with self.assertRaises(ValueError):
            dispatch._run_child([sys.executable, '-c', 'import sys;sys.stdout.write("x"*70000)'],
                                str(self.project), 30)

    def test_a_timed_out_child_takes_its_nested_children_with_it(self):
        pid_file = self.home / 'nested.pid'
        nested = ('import os,pathlib,time;pathlib.Path(%r).write_text(str(os.getpid()));time.sleep(120)'
                  % str(pid_file))
        program = ('import pathlib,subprocess,sys,time;subprocess.Popen([sys.executable,"-c",%r]);'
                   'p=pathlib.Path(%r)\nwhile not p.exists():time.sleep(.01)\ntime.sleep(120)'
                   % (nested, str(pid_file)))
        with self.assertRaises(subprocess.TimeoutExpired):
            dispatch._run_child([sys.executable, '-c', program], str(self.project), 3)
        pid = int(self.wait_until(lambda: pid_file.exists() and pid_file.read_text()))

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        self.wait_until(gone, timeout=10)


class LoggedFailures(DispatchCase):
    def test_a_stalled_preparing_streak_is_logged_once_as_a_failure(self):
        decision = {'state': 'candidate', 'projectRoot': str(self.project)}
        emitted = []
        with mock.patch('memvane.journal.api.select_route', return_value=decision), \
                mock.patch('memvane.diagnostics.api.emit', side_effect=lambda *a, **k: emitted.append((a, k)) or True):
            self.assertTrue(dispatch.stall_event('laya-sync', str(self.project), since=0, observations=21,
                                                 retry_in=300))
            # A project that is gone has no install log to reach.
            self.assertFalse(dispatch.stall_event('laya-sync', str(self.project / 'gone'), since=0,
                                                  observations=1, retry_in=300))
        ((name,), fields), = emitted
        self.assertEqual((name, fields['outcome'], fields['reason'], fields['decision']),
                         ('wake.sync', 'failed', 'target_preparing_stalled', decision))
        self.assertEqual((fields['since'], fields['observations'], fields['retryIn']),
                         ('1970-01-01T00:00:00Z', 21, 300))

    def test_a_dropped_wake_reaches_only_a_project_still_there(self):
        decision = {'state': 'candidate', 'projectRoot': str(self.project)}
        emitted = []
        with mock.patch('memvane.journal.api.select_route', return_value=decision), \
                mock.patch('memvane.diagnostics.api.emit', side_effect=lambda *a, **k: emitted.append((a, k)) or True):
            self.assertTrue(dispatch.drop_event('laya-compile', str(self.project), reason='retry_cap', attempts=6))
            self.assertFalse(dispatch.drop_event('laya-compile', str(self.project / 'gone'), reason='retry_cap'))
        ((name,), fields), = emitted
        self.assertEqual((name, fields['outcome'], fields['reason'], fields['kind'], fields['attempts']),
                         ('wake.drop', 'dropped', 'retry_cap', 'laya-compile', 6))


class WakeEntry(FixtureCase):
    """``entry/run.py wake <kind> <project>``: the child's own argument checks."""

    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        self.project.mkdir()

    def test_only_a_fixed_kind_and_an_owned_real_directory_run(self):
        from memvane.app import wake_entry
        alias = self.home / 'alias'
        alias.symlink_to(self.project)
        plain = self.home / 'file'
        plain.write_text('x')
        cases = (([], 2), (['laya-sync'], 2), (['learning', str(self.project)], 2),
                 (['laya-sync', str(self.project), 'extra'], 2),
                 (['laya-sync', 'relative/project'], 3), (['laya-sync', str(alias)], 3),
                 (['laya-sync', str(self.home / 'missing')], 3), (['laya-sync', str(plain)], 3),
                 (['laya-sync', str(self.project) + '/'], 3))
        with mock.patch('memvane.sync.api.run', side_effect=AssertionError('ran')), \
                mock.patch('memvane.wiki.compile.api.run', side_effect=AssertionError('ran')):
            for argv, code in cases:
                with self.subTest(argv=argv):
                    self.assertEqual(wake_entry.main(argv), code)
            with mock.patch.dict(os.environ, {'QMD_LAYA_WAKE_OWNER_PIPE': '1'}):
                # The owner-pipe mode runs only as its own process group leader.
                self.assertEqual(wake_entry.main(['laya-sync', str(self.project)]), 3)

    def test_each_kind_runs_its_own_worker_and_prints_the_result(self):
        import contextlib
        import io
        from memvane.app import wake_entry
        for kind, target in (('laya-sync', 'memvane.sync.api.run'), ('laya-compile', 'memvane.wiki.compile.api.run')):
            out = io.StringIO()
            with self.subTest(kind=kind), mock.patch(target, return_value={'state': 'unchanged'}) as worker, \
                    contextlib.redirect_stdout(out):
                self.assertEqual(wake_entry.main([kind, str(self.project)]), 0)
            worker.assert_called_once_with(self.project)
            self.assertEqual(json.loads(out.getvalue()), {'state': 'unchanged'})


if __name__ == '__main__':
    unittest.main()
