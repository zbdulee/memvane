#!/usr/bin/env python3
"""Cleanup failure receipts on real disposable files and real product control flow.

Only OS authority is synthetic: process observations, signal delivery, and
launchctl subprocess replies. apply, _stop, _launchctl, safe tree removal and
setup_entry reporting are actual functions. No live ps/kill/launchctl runs.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))
import legacy_cleanup as cleanup
import setup_entry

REAL_APPLY = cleanup.apply
REAL_LAUNCHCTL = cleanup._launchctl
PID = 424242
PORT = 8584


class CleanupPartialChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='legacy-partial-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.home.chmod(0o700)
        self.env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'QMD_SETUP_GUARD_FIXTURE': '1',
            'PYTHONDONTWRITEBYTECODE': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.support = cleanup.plugin_support(self.home)
        self.runtime = self.support / 'runtimes' / 'qmd'
        self.entry = self.write(self.runtime / 'generations' / ('g-' + 'e' * 32) /
                                'qmd-native.mjs', 'synthetic daemon script')
        self.backend = self.support / 'backend'
        self.write(self.backend / ('daemon-%d.pid' % PORT), '%d\n' % PID)
        self.write(self.backend / 'daemon.log', 'synthetic log')
        self.shared = self.support / 'shared-qmd'
        self.write(self.shared / 'store.sqlite', 'synthetic store bytes, not a live DB')
        self.user = self.write(self.home / '.cache' / 'qmd' / 'user-file', 'user-owned')
        self.keeper = self.write(self.support / 'learning' / 'keep', 'must remain')
        self.command = '/opt/fixture/node ' + str(self.entry) + ' mcp --http --port %d' % PORT
        self.live = False
        self.signals = []
        self.commands = []
        self.mount_queries = 0
        self.launch_reply = lambda argv: subprocess.CompletedProcess(argv, 0, '', '')
        # Any unexpected process/network command fails the fixture before OS
        # execution. The real _launchctl still constructs and interprets argv.
        patcher = mock.patch.object(cleanup.subprocess, 'run', self.os_command)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(body, encoding='utf-8')
        path.chmod(0o600)
        return path

    def plist(self, label):
        import plistlib
        raw = plistlib.dumps({'Label': label, 'ProgramArguments': ['/usr/bin/true']},
                             fmt=plistlib.FMT_XML)
        raw = raw.replace(b'<plist version="1.0">',
            ('<!-- ' + cleanup.MARKER + ' -->\n<plist version="1.0">').encode(), 1)
        return self.write(self.home / 'Library' / 'LaunchAgents' / (label + '.plist'),
                          raw.decode('utf-8'))

    def ps(self, pid):
        self.assertEqual(pid, PID)
        return (os.getuid(), self.command) if self.live else None

    def ps_all(self):
        return [(PID, os.getuid(), self.command)] if self.live else []

    def kill(self, pid, sig):
        self.assertEqual((pid, sig), (PID, signal.SIGTERM))
        self.signals.append((pid, int(sig)))
        self.live = False

    def os_command(self, argv, *args, **kwargs):
        self.assertFalse(args)
        if argv == ['/sbin/mount']:
            # A synthetic root-only mount table; the real safe_rmtree mount
            # parser and descriptor/dev/owner checks still execute.
            self.mount_queries += 1
            return subprocess.CompletedProcess(argv, 0, 'fixture-root on / (apfs, local)\n', '')
        self.assertEqual(argv[0], 'launchctl', 'unexpected actual OS command prohibited')
        self.assertIn(argv[1], ('bootout', 'print'))
        self.assertGreater(kwargs.get('timeout', 0), 0)
        self.assertLessEqual(kwargs['timeout'], 30)
        self.commands.append(list(argv))
        result = self.launch_reply(list(argv))
        if not (kwargs.get('text') or kwargs.get('universal_newlines') or kwargs.get('encoding')):
            for field in ('stdout', 'stderr'):
                value = getattr(result, field)
                if isinstance(value, str):
                    setattr(result, field, value.encode('utf-8'))
        return result

    def apply(self, *, ps=None, kill=None, **kwargs):
        return REAL_APPLY(self.home, approved=True, ps=ps or self.ps,
                          ps_all=self.ps_all, kill=kill or self.kill,
                          launchctl=REAL_LAUNCHCTL, sleep=lambda _seconds: None,
                          **kwargs)

    def cli(self, *, ps=None, kill=None):
        # Bind only OS callbacks at the public function boundary. Every result
        # below is produced by the real apply and actual setup_entry.main.
        def configured_apply(**kwargs):
            return REAL_APPLY(self.home, ps=ps or self.ps, ps_all=self.ps_all,
                              kill=kill or self.kill, launchctl=REAL_LAUNCHCTL,
                              sleep=lambda _seconds: None, **kwargs)
        out = io.StringIO()
        with mock.patch.object(cleanup, 'apply', side_effect=configured_apply), \
                redirect_stdout(out):
            code = setup_entry.main(['apply-legacy-cleanup', '--approve-legacy-cleanup'])
        report = json.loads(out.getvalue())
        self.evidence('cli', report, code=code)
        return code, report

    def tree_bytes(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for root in (self.runtime, self.backend, self.shared)
                for path in root.rglob('*') if path.is_file()}

    def evidence(self, case, report, **extra):
        print(json.dumps({'case': case, 'report': report,
                          'signalCallbacks': self.signals, 'launchctlArgv': self.commands,
                          'syntheticMountQueries': self.mount_queries,
                          'remainingTreeFiles': sorted(self.tree_bytes()), **extra},
                         sort_keys=True), flush=True)

    def assert_trees_preserved(self, before):
        self.assertEqual(self.tree_bytes(), before)
        self.assertEqual(self.user.read_text(encoding='utf-8'), 'user-owned')
        self.assertEqual(self.keeper.read_text(encoding='utf-8'), 'must remain')

    def test_signal_sent_then_observation_timeout_has_partial_cli_receipt(self):
        self.live = True
        before = self.tree_bytes()
        def observation(pid):
            if self.signals:
                raise subprocess.TimeoutExpired(['ps', '-p', str(pid)], 5)
            return self.ps(pid)
        code, report = self.cli(ps=observation)
        # Signal acceptance is a change, never proof that the daemon stopped.
        self.assertEqual(self.signals, [(PID, int(signal.SIGTERM))])
        self.assert_trees_preserved(before)
        self.assertEqual(self.commands, [])
        self.assertEqual((code, report['status'], report.get('partial')), (1, 'partial', True))
        self.assertGreater(report['changesApplied'], 0)
        daemon = next(row for row in report['daemons'] if row['pid'] == PID)
        self.assertIs(daemon.get('signalSent'), True)
        self.assertNotEqual(daemon.get('stop'), 'stopped')
        self.assertNotIn(PID, report.get('stoppedPids', []))
        self.assertTrue(any(row.get('pid') == PID and row.get('reason')
                            for row in report['failed']))

    def test_second_bootout_timeout_preserves_completed_plist_and_stops_tree_deletion(self):
        first = self.plist(cleanup.LABELS[0])
        second = self.plist(cleanup.LABELS[1])
        before = self.tree_bytes()
        def replies(argv):
            if argv[1] == 'bootout' and argv[-1] == str(second):
                raise subprocess.TimeoutExpired(argv, 30)
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.launch_reply = replies
        code, report = self.cli()
        self.assertFalse(first.exists(), 'the first real unlink already completed')
        self.assertTrue(second.exists())
        self.assert_trees_preserved(before)
        self.assertEqual((code, report['status'], report.get('partial')), (1, 'partial', True))
        self.assertGreaterEqual(report['changesApplied'], 1)
        self.assertIn({'plist': str(first)}, report['completed'])
        self.assertTrue(any(row.get('plist') == str(second) and row.get('reason')
                            for row in report['failed']))
        agent = next(row for row in report['launchAgents'] if row['plist'] == str(second))
        self.assertEqual(agent['state'], 'failed')

    def test_nonzero_bootout_of_loaded_job_preserves_plist_and_all_trees(self):
        label = cleanup.LABELS[0]
        plist = self.plist(label)
        before = self.tree_bytes()
        def replies(argv):
            if argv[1] == 'bootout':
                return subprocess.CompletedProcess(argv, 5, '', 'Boot-out failed: 5: Input/output error\n')
            self.assertEqual(argv, ['launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)])
            return subprocess.CompletedProcess(argv, 0,
                'gui/%d/%s = { state = running; }\n' % (os.getuid(), label), '')
        self.launch_reply = replies
        report = self.apply()
        self.evidence('loaded-job-nonzero', report)
        self.assertTrue(plist.exists(), 'a failed bootout cannot authorize unlink')
        self.assert_trees_preserved(before)
        self.assertEqual((report['status'], report.get('partial')), ('partial', True))
        self.assertEqual(report['changesApplied'], 0)
        self.assertTrue(any(row.get('plist') == str(plist) and row.get('reason')
                            for row in report['failed']))
        self.assertEqual(next(row['state'] for row in report['launchAgents']
                              if row['plist'] == str(plist)), 'failed')

    def test_already_unloaded_requires_exact_domain_label_absence_evidence(self):
        label = cleanup.LABELS[0]
        plist = self.plist(label)
        uid = os.getuid()
        for case in ('exact-absence', 'other-label', 'other-domain', 'query-error', 'query-timeout'):
            with self.subTest(case=case):
                self.commands = []
                def replies(argv):
                    if argv[1] == 'bootout':
                        return subprocess.CompletedProcess(argv, 3, '', 'Boot-out failed: 3: No such process\n')
                    self.assertEqual(argv, ['launchctl', 'print', 'gui/%d/%s' % (uid, label)])
                    if case == 'query-timeout':
                        raise subprocess.TimeoutExpired(argv, 30)
                    if case == 'query-error':
                        return subprocess.CompletedProcess(argv, 1, '', 'Operation not permitted\n')
                    named = 'com.unrelated.job' if case == 'other-label' else label
                    domain = uid + 1 if case == 'other-domain' else uid
                    return subprocess.CompletedProcess(argv, 113, '',
                        'Bad request.\nCould not find service "%s" in domain for user gui: %d\n' %
                        (named, domain))
                self.launch_reply = replies
                outcome = 'returned'
                try:
                    REAL_LAUNCHCTL(plist)
                except (cleanup.CleanupBlocked, subprocess.TimeoutExpired) as exc:
                    outcome = 'refused'
                    reason = str(exc)
                else:
                    reason = None
                print(json.dumps({'case': case, 'boundary': 'actual-_launchctl',
                    'outcome': outcome, 'reason': reason, 'argv': self.commands}), flush=True)
                self.assertEqual(outcome, 'returned' if case == 'exact-absence' else 'refused')
                self.assertEqual(self.commands, [
                    ['launchctl', 'bootout', 'gui/%d' % uid, str(plist)],
                    ['launchctl', 'print', 'gui/%d/%s' % (uid, label)]])
                self.assertTrue(plist.exists(), '_launchctl itself never removes the file')

    def test_successful_bootout_keeps_the_existing_complete_cleanup_contract(self):
        plist = self.plist(cleanup.LABELS[0])
        report = self.apply()
        self.evidence('successful-cleanup', report)
        self.assertEqual((report['status'], report['partial']), ('cleaned', False))
        self.assertEqual(report['failed'], [])
        self.assertIn({'plist': str(plist)}, report['completed'])
        self.assertEqual(report['changesApplied'], 4)
        self.assertFalse(plist.exists())
        self.assertFalse(self.runtime.exists())
        self.assertFalse(self.shared.exists())
        self.assertFalse(self.backend.exists())
        self.assertTrue(self.user.exists())
        self.assertTrue(self.keeper.exists())
        self.assertEqual(self.signals, [])

    def test_process_table_timeout_after_confirmed_stop_preserves_the_stop_receipt(self):
        self.live = True
        before = self.tree_bytes()
        original = self.ps_all
        observations = []
        def table():
            observations.append(True)
            if len(observations) > 1:
                raise subprocess.TimeoutExpired(['ps', '-axww'], 10)
            return original()
        self.ps_all = table
        code, report = self.cli()
        self.assertEqual((code, report['status'], report['partial']), (1, 'partial', True))
        self.assertEqual(report['changesApplied'], 1)
        self.assertEqual(report['stoppedPids'], [PID])
        self.assertIn({'pid': PID, 'action': 'signal_sent'}, report['completed'])
        self.assertTrue(any(row.get('phase') == 'process_table' and row.get('reason')
                            for row in report['failed']))
        daemon = next(row for row in report['daemons'] if row['pid'] == PID)
        self.assertIs(daemon['signalSent'], True)
        self.assertEqual(daemon['stop'], 'stopped')
        self.assertEqual(self.commands, [])
        self.assert_trees_preserved(before)

    def test_successful_unload_then_unlink_failure_preserves_the_known_change(self):
        plist = self.plist(cleanup.LABELS[0])
        before = self.tree_bytes()
        real_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == plist:
                raise PermissionError(13, 'synthetic unlink denied', str(path))
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', unlink):
            code, report = self.cli()
        self.assertEqual((code, report['status'], report['partial']), (1, 'partial', True))
        self.assertEqual(report['changesApplied'], 1)
        self.assertIn({'plist': str(plist), 'action': 'unloaded'}, report['completed'])
        agent = next(row for row in report['launchAgents'] if row['plist'] == str(plist))
        self.assertEqual((agent['state'], agent['unload']), ('failed', 'unloaded'))
        self.assertTrue(any(row.get('plist') == str(plist) and row.get('reason')
                            for row in report['failed']))
        self.assertTrue(plist.exists())
        self.assert_trees_preserved(before)

    def test_actual_ps_distinguishes_missing_pid_from_failed_observation(self):
        for returncode, stdout, stderr, absent in (
                (1, '', '', True),
                (2, '', 'ps: observation failed\n', False),
                (1, '', 'permission denied\n', False),
                (0, 'not-a-uid missing command', '', False)):
            with self.subTest(returncode=returncode, stderr=stderr, stdout=stdout):
                reply = subprocess.CompletedProcess(['ps'], returncode, stdout, stderr)
                with mock.patch.object(cleanup.subprocess, 'run', return_value=reply) as run:
                    if absent:
                        self.assertIsNone(cleanup._ps(PID))
                    else:
                        with self.assertRaisesRegex(cleanup.CleanupBlocked,
                                '^legacy_cleanup_process_observation_unavailable$'):
                            cleanup._ps(PID)
                    self.assertEqual(run.call_args.args[0],
                                     ['ps', '-p', str(PID), '-o', 'uid=,command='])


if __name__ == '__main__':
    unittest.main()
