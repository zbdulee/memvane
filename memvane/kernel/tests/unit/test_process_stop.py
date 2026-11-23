"""Stopping an owned process group, including one that already ended by itself.

Real children in their own sessions. macOS refuses a signal to a group whose
members have all exited and are not reaped yet; that is not a failed cleanup.
Assertions are return values and exit states; waits go through ``wait_until``.
"""
import os
import signal
import subprocess
import sys
import unittest
from unittest import mock

from memvane.kernel import process
from tests.support.case import wait_until


def child(code):
    return subprocess.Popen([sys.executable, '-c', code], start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def exited_unreaped(code='pass'):
    """A child that has ended and has not been waited for."""
    proc = child(code)
    proc.stdout.read()  # End of output: the child is gone, its exit status still unread.
    proc.stdout.close()
    return proc


class StopOwned(unittest.TestCase):
    def stoppers(self):
        return (('stop_owned', process.stop_owned),
                ('stop_owned_checked', process.stop_owned_checked),
                ('emergency', lambda proc: process.stop_owned_checked(proc, emergency=True)))

    def test_a_child_that_already_ended_is_a_clean_stop(self):
        for name, stop in self.stoppers():
            with self.subTest(stop=name):
                proc = exited_unreaped()
                self.assertTrue(stop(proc))
                self.assertEqual(proc.returncode, 0)

    def test_a_running_child_and_its_descendant_are_stopped(self):
        for name, stop in self.stoppers():
            with self.subTest(stop=name):
                proc = child('import signal,subprocess,sys;'
                             'subprocess.Popen([sys.executable,"-c","import signal;signal.pause()"]);'
                             'print("up",flush=True);signal.pause()')
                self.assertEqual(proc.stdout.readline().strip(), b'up')
                proc.stdout.close()
                self.assertTrue(stop(proc))
                self.assertIsNotNone(proc.returncode)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(proc.pid, 0)

    def test_a_refusal_while_the_leader_runs_is_a_failed_stop(self):
        proc = child('import signal;print("up",flush=True);signal.pause()')
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), b'up')
        with mock.patch.object(process.os, 'killpg', side_effect=PermissionError()), \
                mock.patch.object(process, 'STOP_WAIT', .05):
            self.assertFalse(process.stop_owned(proc))
            self.assertFalse(process.stop_owned_checked(proc))
        self.assertIsNone(proc.poll())

    def test_a_group_that_stays_refused_after_the_leader_ended_is_a_failed_stop(self):
        proc = exited_unreaped()
        with mock.patch.object(process.os, 'killpg', side_effect=PermissionError()), \
                mock.patch.object(process, 'STOP_WAIT', .05):
            self.assertFalse(process.stop_owned(proc))
            self.assertFalse(process.stop_owned_checked(proc))

    def test_the_platform_refusal_this_guards_against_is_only_for_an_unreaped_group(self):
        proc = exited_unreaped()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            answer = 'signalled'
        except PermissionError:
            answer = 'refused'
        except ProcessLookupError:
            answer = 'gone'
        self.assertIn(answer, ('refused',) if sys.platform == 'darwin' else ('signalled', 'gone', 'refused'))
        proc.wait()
        self.assertTrue(wait_until(lambda: _gone(proc.pid), timeout=5))


def _gone(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


if __name__ == '__main__':
    unittest.main()
