import json
import os
import subprocess
import sys
from pathlib import Path

from tests.support.case import FixtureCase
from tests.support.project import make_project


class GenerationWorkerEntryTests(FixtureCase):
    def test_builtin_worker_passes_source_limit_and_writes_card(self):
        project = make_project(self.home, git=False)
        source = project.write_note('docs/source.md', 'Durable source facts.\n' + 'x' * 14000)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        queue.parent.mkdir(parents=True)
        job = {'ts': '2026-08-03T00:00:00Z', 'trigger': 'post_tool_source', 'engine': 'claude',
               'cwd': str(project.root), 'source': {'kind': 'file', 'path': 'docs/source.md',
                                                     'collection': project.raw_name}}
        queue.write_text(json.dumps(job) + '\n', encoding='utf-8')
        log = self.tmp / 'fake-cli-args.json'
        fake = self.tmp / 'claude-fake'
        fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                        'open(os.environ["FIXTURE_ARG_LOG"],"a").write(json.dumps(sys.argv[1:])+"\\n")\n'
                        'print(json.dumps({"candidates":[{"title":"Dispatch Guard Decision",'
                        '"summary":"A durable synthetic project fact.","suggestedType":"decision",'
                        '"confidence":"high","targetPath":".memvane/wiki/decisions/dispatch-guard-decision.md"}]}))\n')
        fake.chmod(0o700)
        root = Path(__file__).resolve().parents[5]
        env = dict(os.environ, QMD_EXTRACTOR_CLAUDE_BIN=str(fake), FIXTURE_ARG_LOG=str(log))
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'wiki-compile-worker',
                                 '--cwd', str(project.root), '--json', '--flush-all'],
                                text=True, capture_output=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(log.exists(), result.stdout + result.stderr)
        prompt = ' '.join(json.loads(log.read_text().splitlines()[0]))
        self.assertIn('SOURCE FILE: docs/source.md (truncated: true', prompt)
        self.assertIn('up to 120 lines and within the CARD BUDGET', prompt)
        self.assertIn('x' * (12000 - len('Durable source facts.\n')), prompt)
        self.assertNotIn('x' * (12001 - len('Durable source facts.\n')), prompt)
        self.assertTrue((project.wiki / 'decisions/dispatch-guard-decision.md').exists(), result.stdout)
        candidates = project.root / '.memvane/compile/candidates.jsonl'
        self.assertNotIn('missing_extractor', candidates.read_text(encoding='utf-8'))
