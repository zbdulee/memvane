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
        payload = json.dumps({'source': {'path': 'docs/a.md', 'content': 'PRIVATE_BODY'}, 'wiki': {},
                              '_qmd': {'reasoningEffort': {'requested': 'high'}}})
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                 'extractor-' + name], input=payload, text=True,
                                capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response['candidates'][0]['title'], 'fixture')
        self.assertNotIn('PRIVATE_BODY', result.stdout)
        audit = response['_qmd']['reasoningEffort']
        self.assertEqual(audit['requested'], 'high')
        self.assertEqual(audit['status'], 'unsupported' if name == 'hermes' else 'applied')
        self.assertEqual(audit['applied'], None if name == 'hermes' else 'high')
        observed = json.loads(log.read_text(encoding='utf-8'))
        if name == 'claude':
            self.assertIn('--effort', observed['args'])
            self.assertIn('high', observed['args'])
        elif name == 'codex':
            self.assertIn('model_reasoning_effort=high', observed['args'])
        else:
            self.assertFalse(any('effort' in arg for arg in observed['args']))
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

    def test_claude_verify_entry_uses_verification_prompt(self):
        log = self.tmp / 'verify-args.json'
        fake = self.tmp / 'fake-claude-verify'
        fake.write_text('#!/usr/bin/env python3\n'
                        'import json,os,sys\n'
                        'open(os.environ["FIXTURE_ADAPTER_LOG"],"w").write(json.dumps(sys.argv[1:]))\n'
                        'print(\'{"verdict":"pass","claims":[{"claim":"c1","supported":true,"quote":"q","sourcePath":"docs/s.md"}],"reasons":[]}\')\n')
        fake.chmod(0o755)
        env = dict(os.environ, FIXTURE_ADAPTER_LOG=str(log), QMD_EXTRACTOR_CLAUDE_BIN=str(fake))
        root = Path(__file__).resolve().parents[5]
        payload = {'task': 'verify', 'card': {'path': 'wiki/x.md', 'content': 'CARD_BODY'},
                   'sources': [{'path': 'docs/s.md', 'content': 'SRC_BODY', 'truncated': False}]}
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'extractor-claude'],
                                input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['verdict'], 'pass')
        self.assertTrue(json.loads(result.stdout)['claims'][0]['supported'])
        prompt = ' '.join(json.loads(log.read_text()))
        self.assertIn('REFUTE', prompt)
        self.assertIn('CARD_BODY', prompt)
        self.assertNotIn('wiki candidates', prompt)

    def test_missing_claude_cli_returns_127(self):
        root = Path(__file__).resolve().parents[5]
        env = dict(os.environ, QMD_EXTRACTOR_CLAUDE_BIN='/nonexistent/claude-synthetic', PATH='/usr/bin:/bin')
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'extractor-claude'],
                                input='{"source":{"path":"x","content":"y"},"wiki":{}}',
                                text=True, capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 127)

    def test_signalled_cli_reports_bounded_audit_without_prompt(self):
        fake = self.tmp / 'signal-codex'
        fake.write_text('#!/bin/sh\nkill -KILL $$\n', encoding='utf-8')
        fake.chmod(0o700)
        root = Path(__file__).resolve().parents[5]
        env = dict(os.environ, QMD_EXTRACTOR_CODEX_BIN=str(fake))
        payload = {'task': 'verify', 'card': {'content': 'PRIVATE_SYNTHETIC_BODY'}, 'sources': []}
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'extractor-codex'],
                                input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('extractor_cli_failed: engine=codex returncode=-9', result.stderr)
        audit = next(json.loads(line) for line in result.stderr.splitlines() if line.startswith('{'))
        self.assertEqual((audit['event'], audit['signal'], audit['timedOut'], audit['terminationOrigin']),
                         ('extractor_cli_exit', 'SIGKILL', False, 'unknown'))
        self.assertGreater(audit['pid'], 0)
        self.assertGreater(audit['parentPid'], 0)
        self.assertEqual(audit['executable'], str(fake.resolve()))
        self.assertNotIn('PRIVATE_SYNTHETIC_BODY', result.stderr)

    def test_bare_override_audit_reports_resolved_executable(self):
        fake = self.tmp / 'fake-codex'
        fake.write_text('#!/bin/sh\nexit 7\n', encoding='utf-8')
        fake.chmod(0o700)
        root = Path(__file__).resolve().parents[5]
        env = dict(os.environ, QMD_EXTRACTOR_CODEX_BIN='fake-codex',
                   PATH=str(self.tmp) + os.pathsep + os.environ.get('PATH', ''))
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'extractor-codex'],
                                input=json.dumps({'task': 'verify', 'card': {}, 'sources': []}),
                                text=True, capture_output=True, env=env, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('extractor_cli_failed: engine=codex returncode=7', result.stderr)
        audit = next(json.loads(line) for line in result.stderr.splitlines() if line.startswith('{'))
        self.assertEqual(audit['command'], 'fake-codex')
        self.assertEqual(audit['executable'], str(fake.resolve()))
