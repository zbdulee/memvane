import json
import os
from pathlib import Path
import subprocess
import sys

from tests.support.case import FixtureCase


class ExtractorEntryTests(FixtureCase):
    def check_adapter(self, name, expected):
        log = self.tmp / (name + '.json')
        fake = self.tmp / ('fake-' + name)
        fake.write_text('#!/usr/bin/env python3\n'
                        'import json,os,sys\n'
                        'with open(os.environ["FIXTURE_ADAPTER_LOG"], "w") as f:\n'
                        ' json.dump({"args":sys.argv[1:], "cwd":os.getcwd(), '
                        '"sandbox":os.environ.get("QMD_SANDBOX")}, f)\n'
                        'print(\'{"candidates":[{"title":"fixture"}]}\')\n', encoding='utf-8')
        fake.chmod(0o755)
        env = dict(os.environ, FIXTURE_ADAPTER_LOG=str(log))
        env['QMD_EXTRACTOR_' + name.upper() + '_BIN'] = str(fake)
        root = Path(__file__).resolve().parents[5]
        payload = json.dumps({'source': {'path': 'docs/a.md', 'content': 'body'}, 'wiki': {}})
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                 'extractor-' + name], input=payload, text=True,
                                capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['candidates'][0]['title'], 'fixture')
        observed = json.loads(log.read_text(encoding='utf-8'))
        self.assertEqual(observed['sandbox'], '1')
        self.assertNotEqual(observed['cwd'], str(root))
        for flag in expected:
            self.assertIn(flag, observed['args'])

    def test_claude_entry_isolated(self):
        self.check_adapter('claude', ['--safe-mode', '--no-session-persistence'])

    def test_codex_entry_isolated(self):
        self.check_adapter('codex', ['read-only', '--ephemeral', '--ignore-user-config', '--ignore-rules'])

    def test_hermes_entry_isolated(self):
        self.check_adapter('hermes', ['-z', '--safe-mode', '--ignore-user-config', '--ignore-rules'])
