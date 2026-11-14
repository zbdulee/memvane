"""A frozen backfill job cannot authorize changed source bytes."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from memvane.wiki.intake import wiki_backfill as backfill
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class BackfillWorkerBoundary(FixtureCase):
    def test_worker_does_not_extract_new_bytes_under_frozen_authority(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'frozen source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = {project.raw_name + '/docs/note.md': revision}
        config = expanded(project, compile={'mode': 'auto-wiki',
                          'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
        row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'codex'})
        self.assertIsNotNone(row)
        self.assertEqual(backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)['dispatched'], 1)
        source.write_text('new bytes require a new grant\n')
        marker = self.home / 'extractor-was-called'
        fake = self.home / 'fake-extractor'
        fake.write_text('#!/usr/bin/env python3\nimport os\nopen(os.environ["FIXTURE_MARKER"], "w").write("called")\nprint("{}")\n')
        fake.chmod(0o700)
        root = Path(__file__).resolve().parents[5]
        env = {**os.environ, 'QMD_ENGINE': 'codex', 'FIXTURE_MARKER': str(marker),
               'QMD_EXTRACTOR_CODEX_BIN': str(fake), 'QMD_EXTRACTOR_CLAUDE_BIN': str(fake),
               'QMD_EXTRACTOR_HERMES_BIN': str(fake)}
        child = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all'],
                               cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse(marker.exists(), 'changed bytes reached the extractor')
        progress = backfill.summary(backfill._load(decision))
        self.assertEqual((progress['state'], progress['skipped']), ('complete_with_skips', 1))

    def test_changed_install_scope_or_incarnation_never_invokes_extractor(self):
        import copy
        import json
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'frozen source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = {project.raw_name + '/docs/note.md': revision}
        config = expanded(project, compile={'mode': 'auto-wiki',
                          'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
        row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'codex'})
        backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        job = json.loads(queue.read_text().splitlines()[0])
        job.pop('initialBackfill')
        marker = self.home / 'extractor-was-called'
        fake = self.home / 'fake-extractor'
        fake.write_text('#!/usr/bin/env python3\nimport os\nopen(os.environ["FIXTURE_MARKER"], "w").write("called")\nprint("{}")\n')
        fake.chmod(0o700)
        root = Path(__file__).resolve().parents[5]
        env = {**os.environ, 'QMD_ENGINE': 'codex', 'FIXTURE_MARKER': str(marker),
               'QMD_EXTRACTOR_CODEX_BIN': str(fake), 'QMD_EXTRACTOR_CLAUDE_BIN': str(fake),
               'QMD_EXTRACTOR_HERMES_BIN': str(fake)}
        for field in ('installId', 'scopeId', 'storeIncarnation'):
            with self.subTest(field=field):
                changed = copy.deepcopy(job)
                changed['selectedRoute'][field] = 'f' * 32
                queue.write_text(json.dumps(changed) + '\n')
                child = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                        'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all'],
                                       cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertFalse(marker.exists(), 'mismatched route reached the extractor')
                candidates = project.root / '.memvane/compile/candidates.jsonl'
                self.assertIn('selected_route_identity_changed', candidates.read_text())
        self.assertEqual(backfill.summary(backfill._load(decision))['dispatched'], 1)

    def test_known_host_failure_never_tries_a_different_cli(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'frozen source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = {project.raw_name + '/docs/note.md': revision}
        config = expanded(project, compile={'mode': 'auto-wiki',
                          'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
        row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'codex'})
        backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)
        calls = self.home / 'calls.txt'
        scripts = {}
        for engine in ('codex', 'claude', 'hermes'):
            fake = self.home / ('fake-' + engine)
            fake.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                            'open(os.environ["FIXTURE_CALLS"], "a").write(' + repr(engine + '\n') + ')\n'
                            + ('sys.exit(127)\n' if engine == 'codex' else 'print("{}")\n'))
            fake.chmod(0o700)
            scripts[engine] = str(fake)
        root = Path(__file__).resolve().parents[5]
        env = {**os.environ, 'QMD_ENGINE': 'codex', 'FIXTURE_CALLS': str(calls),
               'QMD_EXTRACTOR_CODEX_BIN': scripts['codex'], 'QMD_EXTRACTOR_CLAUDE_BIN': scripts['claude'],
               'QMD_EXTRACTOR_HERMES_BIN': scripts['hermes']}
        child = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all'],
                               cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(calls.read_text().splitlines(), ['codex'])
        self.assertEqual(backfill.summary(backfill._load(decision))['dispatched'], 1)

    def test_unknown_host_uses_real_attempts_once_then_records_successful_producer(self):
        import json
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'A durable synthetic source fact.\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = {project.raw_name + '/docs/note.md': revision}
        config = expanded(project, compile={'mode': 'auto-wiki',
                          'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
        row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'unknown'})
        backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)
        calls = self.home / 'calls.txt'
        scripts = {}
        for engine in ('codex', 'claude', 'hermes'):
            fake = self.home / ('fake-' + engine)
            success = {'candidates': [{'title': 'Synthetic Durable Fact',
                       'summary': 'A durable synthetic source fact.', 'confidence': 'high',
                       'suggestedType': 'concept', 'targetPath': '.memvane/wiki/concepts/synthetic.md'}]}
            body = ('sys.exit(127)\n' if engine == 'codex'
                    else 'print(' + repr(json.dumps(success)) + ')\n')
            fake.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                            'open(os.environ["FIXTURE_CALLS"], "a").write(' + repr(engine + '\n') + ')\n' + body)
            fake.chmod(0o700)
            scripts[engine] = str(fake)
        root = Path(__file__).resolve().parents[5]
        env = {**os.environ, 'QMD_ENGINE': '', 'FIXTURE_CALLS': str(calls),
               'QMD_EXTRACTOR_CODEX_BIN': scripts['codex'], 'QMD_EXTRACTOR_CLAUDE_BIN': scripts['claude'],
               'QMD_EXTRACTOR_HERMES_BIN': scripts['hermes']}
        child = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all'],
                               cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(child.returncode, 0, child.stderr)
        called = calls.read_text().splitlines()
        self.assertEqual(called[:2], ['codex', 'claude'])
        self.assertEqual(called.count('codex'), 1)
        self.assertNotIn('hermes', called)
        candidates = project.root / '.memvane/compile/candidates.jsonl'
        records = [json.loads(line) for line in candidates.read_text().splitlines()]
        attempts = [record for record in records if record.get('reason') == 'selected']
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['attemptedEngines'], ['codex', 'claude'])
        self.assertEqual(attempts[0]['selectedEngine'], 'claude')
        self.assertEqual(backfill.summary(backfill._load(decision))['settled'], 1)
        card = project.wiki / 'concepts/synthetic.md'
        self.assertTrue(card.exists(), child.stdout + child.stderr)

    def test_unknown_resource_deferral_preserves_job_without_other_host_attempt(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'frozen source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = {project.raw_name + '/docs/note.md': revision}
        config = expanded(project, compile={'mode': 'auto-wiki',
                          'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
        row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'unknown'})
        backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)
        calls = self.home / 'calls.txt'
        scripts = {}
        for engine in ('codex', 'claude', 'hermes'):
            fake = self.home / ('fake-' + engine)
            fake.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                            'open(os.environ["FIXTURE_CALLS"], "a").write(' + repr(engine + '\n') + ')\n'
                            + ('sys.exit(75)\n' if engine == 'codex' else 'print("{}")\n'))
            fake.chmod(0o700)
            scripts[engine] = str(fake)
        root = Path(__file__).resolve().parents[5]
        env = {**os.environ, 'QMD_ENGINE': '', 'FIXTURE_CALLS': str(calls),
               'QMD_EXTRACTOR_CODEX_BIN': scripts['codex'], 'QMD_EXTRACTOR_CLAUDE_BIN': scripts['claude'],
               'QMD_EXTRACTOR_HERMES_BIN': scripts['hermes']}
        child = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all'],
                               cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(calls.read_text().splitlines(), ['codex'])
        self.assertEqual(backfill.summary(backfill._load(decision))['dispatched'], 1)
        self.assertTrue((project.root / '.memvane/compile/source-queue.jsonl').read_text().strip())

    def test_unknown_exhaustion_is_skipped_without_rebilling_cycle(self):
        import json
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'frozen source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = {project.raw_name + '/docs/note.md': revision}
        config = expanded(project, compile={'mode': 'auto-wiki',
                          'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
        row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'unknown'})
        backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)
        calls = self.home / 'calls.txt'
        scripts = {}
        for engine in ('codex', 'claude', 'hermes'):
            fake = self.home / ('fake-' + engine)
            fake.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                            'open(os.environ["FIXTURE_CALLS"], "a").write(' + repr(engine + '\n') + ')\n'
                            'sys.exit(1)\n')
            fake.chmod(0o700)
            scripts[engine] = str(fake)
        root = Path(__file__).resolve().parents[5]
        env = {**os.environ, 'QMD_ENGINE': '', 'FIXTURE_CALLS': str(calls),
               'QMD_EXTRACTOR_CODEX_BIN': scripts['codex'], 'QMD_EXTRACTOR_CLAUDE_BIN': scripts['claude'],
               'QMD_EXTRACTOR_HERMES_BIN': scripts['hermes']}
        command = [sys.executable, '-B', str(root / 'entry/run.py'),
                   'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all']
        child = subprocess.run(command, cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(calls.read_text().splitlines(), ['codex', 'claude', 'hermes'])
        progress = backfill.summary(backfill._load(decision))
        self.assertEqual((progress['state'], progress['skipped'], progress['settled']),
                         ('complete_with_skips', 1, 0))
        candidates = project.root / '.memvane/compile/candidates.jsonl'
        rows = [json.loads(line) for line in candidates.read_text().splitlines()]
        self.assertTrue(any(row.get('reason') == 'unknown_host_exhausted' for row in rows))
        retry = subprocess.run(command, cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(calls.read_text().splitlines(), ['codex', 'claude', 'hermes'])

    def test_budget_one_and_two_resume_only_untried_unknown_hosts(self):
        import copy
        import json
        for remaining in (1, 2):
            with self.subTest(remaining=remaining):
                project = make_project(self.home, name='budget%d' % remaining,
                                       dirname='budget-project-%d' % remaining)
                special = project.write_note('docs/special.md', 'SPECIAL_BACKFILL unique source\n')
                pads = [project.write_note('docs/pad%d.md' % index, 'padding source %d\n' % index)
                        for index in range(10 - remaining)]
                project.git('add', '--', '.memvane/settings.json', 'docs')
                project.git('commit', '-qm', 'fixture')
                install = build_install(self, project, install_id=str(remaining + 4) * 32)
                decision = install.select()
                revision = hashlib.sha256(special.read_bytes()).hexdigest()
                snapshot = {project.raw_name + '/docs/special.md': revision}
                config = expanded(project, compile={'mode': 'auto-wiki',
                                  'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
                row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'unknown'})
                backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)
                queue = project.root / '.memvane/compile/source-queue.jsonl'
                special_job = json.loads(queue.read_text().splitlines()[0])
                jobs = []
                for source in pads:
                    job = copy.deepcopy(special_job)
                    job.pop('initialBackfill')
                    job['engine'] = 'codex'
                    job['source']['path'] = source.relative_to(project.root).as_posix()
                    job['contentRevision'] = hashlib.sha256(source.read_bytes()).hexdigest()
                    jobs.append(job)
                queue.write_text(''.join(json.dumps(job) + '\n' for job in jobs + [special_job]))
                calls = self.home / ('calls-%d.jsonl' % remaining)
                scripts = {}
                for engine in ('codex', 'claude', 'hermes'):
                    fake = self.home / ('fake-%d-%s' % (remaining, engine))
                    fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                        'special=any("SPECIAL_BACKFILL" in arg for arg in sys.argv[1:])\n'
                        'open(os.environ["FIXTURE_CALLS"],"a").write(json.dumps({"engine":'
                        + repr(engine) + ',"special":special})+"\\n")\n'
                        + ('\nif special: sys.exit(127)\n' if engine in ('codex', 'claude') else '\n')
                        + 'print(json.dumps({"candidates": []}))\n')
                    fake.chmod(0o700)
                    scripts[engine] = str(fake)
                root = Path(__file__).resolve().parents[5]
                env = {**os.environ, 'QMD_ENGINE': '', 'FIXTURE_CALLS': str(calls),
                       'QMD_EXTRACTOR_CODEX_BIN': scripts['codex'], 'QMD_EXTRACTOR_CLAUDE_BIN': scripts['claude'],
                       'QMD_EXTRACTOR_HERMES_BIN': scripts['hermes']}
                command = [sys.executable, '-B', str(root / 'entry/run.py'),
                           'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all']
                first = subprocess.run(command, cwd=str(project.root), env=env,
                                       capture_output=True, text=True, timeout=90)
                self.assertEqual(first.returncode, 0, first.stderr)
                kept = [json.loads(line) for line in queue.read_text().splitlines()]
                self.assertEqual(len(kept), 1, first.stdout + first.stderr)
                expected = ['codex', 'claude'][:remaining]
                self.assertEqual(kept[0]['_fallbackAttempts']['engines'], expected)
                first_calls = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(len(first_calls), 10)
                second = subprocess.run(command, cwd=str(project.root), env=env,
                                        capture_output=True, text=True, timeout=90)
                self.assertEqual(second.returncode, 0, second.stderr)
                all_calls = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual([call['engine'] for call in all_calls if call['special']],
                                 ['codex', 'claude', 'hermes'])
                self.assertEqual(backfill.summary(backfill._load(decision))['settled'], 1)
                self.assertFalse(queue.read_text().strip())
