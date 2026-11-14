import json
import subprocess
import sys
from pathlib import Path

from tests.support.case import FixtureCase
from tests.support.project import make_project


class CompileSecretBoundaryTests(FixtureCase):
    def test_identifier_rich_card_is_kept_and_synthetic_secrets_are_rejected(self):
        project = make_project(self.home, git=False)
        root = Path(__file__).resolve().parents[5]
        def compile_card(title, summary, slug):
            payload = {'trigger': 'manual', 'title': title, 'summary': summary,
                       'suggestedType': 'concept', 'confidence': 'high',
                       'targetPath': '.memvane/wiki/concepts/' + slug + '.md'}
            result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'wiki-compile',
                                     '--cwd', str(project.root)], input=json.dumps(payload), text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return project.wiki / 'concepts' / (slug + '.md')
        benign = ('`compile.extractor.timeout` 30; `maxSourceChars` 12000; '
                  '`token: <REDACTED>`; `API_KEY=$OPENAI_API_KEY`; `secret: null`; '
                  'payload schema: {token: string}; token = {{value}}; api_key: ***; '
                  'secret: ...; GITHUB_TOKEN=${GITHUB_TOKEN}; exit 127')
        card = compile_card('Extractor Auth Config', benign, 'extractor-auth-config')
        self.assertTrue(card.exists())
        text = card.read_text(encoding='utf-8')
        for word in ('compile.extractor.timeout', 'maxSourceChars', '12000', '$OPENAI_API_KEY', 'exit 127'):
            self.assertIn(word, text)
        self.assertNotIn('redactions:\n  - secret_like', text)
        secrets = ('sk-1234567890abcdef1234567890abcdef',
                   'token: ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6',
                   'API_KEY=AKIAIOSFODNN7EXAMPLE', 'secret: hunter2correcthorsebattery',
                   'token: "eyJhbGciOiJIUzI1NiJ9.payload.sig"',
                   'api-key: 8f14e45fceea167a5a36dedd4bea2543')
        for number, secret in enumerate(secrets):
            with self.subTest(number=number):
                card = compile_card('Leaky Card %d' % number, secret, 'leaky-card-%d' % number)
                self.assertFalse(card.exists())
        ledger = (project.root / '.memvane/compile/candidates.jsonl').read_text(encoding='utf-8')
        records = {row['title']: row for row in map(json.loads, ledger.splitlines())}
        self.assertEqual(records['Extractor Auth Config']['summary'], benign)
        self.assertEqual(records['Extractor Auth Config']['redactions'], [])
        for number, secret in enumerate(secrets):
            with self.subTest(number=number):
                row = records['Leaky Card %d' % number]
                self.assertIn('[REDACTED]', row['summary'])
                self.assertIn('secret_like', json.dumps(row))
                self.assertNotIn(secret, ledger)
