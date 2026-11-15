import contextlib
import io
import json
import os
import subprocess
from unittest.mock import Mock, patch

from tests.support.case import FixtureCase
from memvane.wiki.engines.extractors import lib


class AdapterIsolationTests(FixtureCase):
    def test_path_resolution_prefers_path_then_fnm_then_explicit_override(self):
        preferred = self.home / 'bin'
        preferred.mkdir()
        fallback = self.home / '.local/share/fnm/node-versions/v99.0.0/installation/bin'
        fallback.mkdir(parents=True)
        for folder in (preferred, fallback):
            executable = folder / 'codex'
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o700)
        with patch.dict(os.environ, {'HOME': str(self.home), 'PATH': str(preferred)}, clear=True):
            self.assertEqual(lib.resolve_bin('codex', 'FIXTURE_OVERRIDE'), str(preferred / 'codex'))
            os.environ['PATH'] = str(self.home / 'missing')
            self.assertEqual(lib.resolve_bin('codex', 'FIXTURE_OVERRIDE'), str(fallback / 'codex'))
            os.environ['FIXTURE_OVERRIDE'] = '/explicit/codex'
            self.assertEqual(lib.resolve_bin('codex', 'FIXTURE_OVERRIDE'), '/explicit/codex')

    def test_audit_records_child_identity_without_prompt_or_stderr(self):
        from memvane.runtime import api as runtime
        for code in (0, -9):
            with self.subTest(code=code):
                child = Mock(pid=12345, returncode=code, stdout=None, stderr=None)
                child.communicate.return_value = ('{"candidates":[]}', 'PRIVATE_CREDENTIAL')
                child.poll.return_value = code
                stderr = io.StringIO()
                with patch.object(runtime, 'provider_popen', return_value=child) as spawn, \
                        contextlib.redirect_stderr(stderr):
                    out, status, private = lib._run_isolated_admitted(['/synthetic/cli', 'PRIVATE_PROMPT'], 10)
                self.assertEqual(status, code)
                self.assertEqual(private, 'PRIVATE_CREDENTIAL')
                self.assertEqual(spawn.call_args.kwargs['env']['QMD_SANDBOX'], '1')
                audit = json.loads(stderr.getvalue())
                self.assertEqual(audit['event'], 'extractor_cli_exit')
                self.assertEqual(audit['pid'], 12345)
                self.assertEqual(audit['childReturncode'], code)
                self.assertEqual(audit['signal'], 'SIGKILL' if code == -9 else None)
                self.assertNotIn('PRIVATE_PROMPT', stderr.getvalue())
                self.assertNotIn('PRIVATE_CREDENTIAL', stderr.getvalue())
                self.assertEqual(out, '{"candidates":[]}')

    def test_timeout_has_own_cleanup_origin(self):
        from memvane.runtime import api as runtime
        from memvane.kernel import process
        child = Mock(pid=12345, returncode=-9, stdout=None, stderr=None)
        child.communicate.side_effect = [subprocess.TimeoutExpired('synthetic', 1), ('', '')]
        child.poll.return_value = -9
        stderr = io.StringIO()
        with patch.object(runtime, 'provider_popen', return_value=child), \
                patch.object(process, 'stop_owned') as stop, contextlib.redirect_stderr(stderr):
            _, status, _ = lib._run_isolated_admitted(['/synthetic/cli'], 1)
        self.assertEqual(status, 124)
        stop.assert_called_once_with(child)
        audit = json.loads(stderr.getvalue())
        self.assertTrue(audit['timedOut'])
        self.assertEqual(audit['terminationOrigin'], 'adapter_timeout')
