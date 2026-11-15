import contextlib
import io
import json
import unittest
from unittest.mock import patch

from memvane.wiki.engines.extractors import claude_adapter, codex_adapter, lib


class AdapterEffortTests(unittest.TestCase):
    def run_adapter(self, engine, outcomes):
        payload = {'source': {'path': 'docs/x.md', 'content': 'secret source'}, 'wiki': {},
                   '_qmd': {'reasoningEffort': {'requested': 'high'}}}
        builder = claude_adapter.build_command if engine == 'claude' else codex_adapter.build_command
        out, err = io.StringIO(), io.StringIO()
        with patch.object(lib, 'read_payload', return_value=payload), \
                patch.object(lib, 'resolve_bin', return_value='/synthetic/cli'), \
                patch.object(lib, 'run_isolated_detailed', side_effect=outcomes) as invoke, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lib.run_adapter(engine, 'UNUSED', builder, engine=engine, supports_effort=True)
        return code, out.getvalue(), err.getvalue(), invoke.call_args_list

    def test_effort_option_rejection_retries_once_without_effort(self):
        code, output, _, calls = self.run_adapter('claude', [
            (None, 2, "unknown option '--effort'"), ('{"candidates":[]}', 0, '')])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn('--effort', calls[0].args[0])
        self.assertNotIn('--effort', calls[1].args[0])
        self.assertEqual(json.loads(output)['_qmd']['reasoningEffort'],
                         {'requested': 'high', 'applied': None,
                          'status': 'retried_without_effort', 'reason': 'effort_option_rejected'})
        self.assertNotIn('secret source', output)

    def test_generic_or_other_engine_rejection_does_not_retry(self):
        for engine, stderr in (('claude', 'network failure'),
                               ('codex', "unknown option '--effort'")):
            with self.subTest(engine=engine):
                code, _, _, calls = self.run_adapter(engine, [(None, 2, stderr)])
                self.assertEqual(code, 2)
                self.assertEqual(len(calls), 1)
